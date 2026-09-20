#include "audition_retry_adapter.hpp"
#include "screen_context.hpp"
#include "pointer_identity.hpp"
#include "audition_identity.hpp"

namespace gkms::bridge {
namespace {
int number(Runtime& r,void* o,const char* getter){return r.unbox<int>(r.getter(o,getter));}
bool flag(Runtime& r,void* o,const char* getter){return r.unbox<bool>(r.getter(o,getter));}
bool active(Runtime& r,void* o){return o&&flag(r,r.getter(o,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* b){return active(r,b)&&flag(r,b,"get_IsEnabled")&&!flag(r,b,"get_IsDisabled")&&r.read_object_field(b,"onClickedCallback");}
bool retry_sheet(const std::string& type){return type=="ProduceAuditionContinueWarningSheetPresenter"||
    type=="ProduceAuditionContinueConfirmSheetPresenter"||type=="ProduceAuditionContinueSelectConfirmSheetPresenter";}
void* execute(Runtime& r,void* sheet){return r.getter(r.getter(r.read_object_field(sheet,"_view"),"get_CommonView"),"get_ExecuteButton");}
json quota(Runtime& r){
    auto parent=active_screen(r);
    // Both presenters retain the same AuditionBattleResultScreenModel and
    // normal continue sheets; their success/result presentation differs.
    if(!parent||!native_audition_result_parent(r.class_name(r.object_class(parent))))return nullptr;
    auto model=r.read_object_field(parent,"_model");
    const int free=number(r,model,"get_RemainFreeContinueCount");
    const int remaining=number(r,model,"get_RemainContinueCount");
    auto master=r.klass("Assembly-CSharp","Campus.Common.Master","MasterManager");
    auto setting=r.invoke(r.method(master,"get_Setting",0,0x060188AC),nullptr);
    auto item_id_value=r.getter(setting,"get_ProduceContinueItemID");
    const auto item_id=r.string(item_id_value);
    auto user=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto items=r.invoke(r.method(user,"get_UserItemList",0),nullptr);
    const auto balance=r.unbox<std::int64_t>(r.invoke(r.method(r.object_class(items),"GetItemQuantity",1,0x06016A15),items,{item_id_value}));
    const bool ticket=item_id=="item-produce_continue-1";
    const bool ticket_available=native_single_ticket_retry(item_id,balance,free,remaining,1,1);
    json result={{"parent_instance_id",pointer_identity(parent)},{"free_count",free},{"remaining_count",remaining},
        {"has_continue_item",flag(r,model,"get_HasContinueItem")},{"free_retry_available",native_free_retry_available(free,remaining)},
        {"ticket_item_id",item_id},{"ticket_balance",balance},{"ticket_cost",free>0?json(0):(ticket&&free==0?json(1):json(nullptr))},
        {"ticket_retry_available",ticket_available},{"price",free>0?json(0):(ticket&&free==0?json(1):json(nullptr))},
        {"price_source","native Setting continue item and current inventory; normal retry consumes one ticket when free count is zero"}};
    auto progress=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    auto schedule=r.getter(progress,"get_CurrentSchedule");
    result["entry_number"]=number(r,progress,"get_StepSelectNumber");
    result["failed_number"]=nullptr;result["failed_step_number"]=nullptr;result["failed_step_type"]=nullptr;
    if(schedule){
        json row={{"step_number",number(r,schedule,"get_StepNumber")},{"selected_step_type",number(r,schedule,"get_SelectedStepType")},
            {"step_select_number",number(r,schedule,"get_StepSelectNumber")}};
        result["failed_number"]=current_audition_number(number(r,progress,"get_StepNumber"),number(r,progress,"get_StepType"),row);
        if(!result.at("failed_number").is_null()){
            result["failed_step_number"]=row.at("step_number");result["failed_step_type"]=row.at("selected_step_type");
        }
    }
    return result;
}
json action(const char* id,json target){target["action_id"]=id;return {{"action_id",id},{"target",target}};}
void click(Runtime& r,void* b){if(!clickable(r,b))throw std::runtime_error("retry control no longer enabled");r.invoke(r.method(r.object_class(b),"OnClickedHandler",0),b);}
}
bool append_audition_retry_actions(Runtime& r,void* sheet,const std::string& type,json& snapshot){
    if(!retry_sheet(type))return false;
    // Clear generic confirmation: free and explicitly bounded ticket inputs are separate actions.
    snapshot["legal_actions"]=json::array();snapshot["surface"]="audition_retry";
    auto state=quota(r);snapshot["actions_complete"]=!state.is_null()&&snapshot.at("blockers").empty();
    if(state.is_null()){snapshot["ui_state"]={{"data_ready",false}};return true;}
    state["data_ready"]=true;state["candidates"]=json::array();state["selected_number"]=nullptr;
    auto view=r.read_object_field(sheet,"_view");
    auto base=json{{"sheet_type",type},{"sheet_instance_id",pointer_identity(sheet)},
        {"parent_instance_id",state.at("parent_instance_id")},{"free_count",state.at("free_count")},
        {"remaining_count",state.at("remaining_count")},{"price",state.at("price")}};
    if(type=="ProduceAuditionContinueSelectConfirmSheetPresenter"){
        auto model=r.getter(sheet,"get_Model");
        state["selected_number"]=number(r,model,"get_SelectedAuditionNumber");
        auto rows=r.enumerate(r.getter(model,"get_AuditionDifficultyList"),32);
        auto cells=r.enumerate(r.read_object_field(view,"_selectAuditionlist"),32);
        for(std::size_t index=0;index<rows.size();++index){
            auto row=rows[index];
            json identity={{"index",index},{"number",number(r,row,"get_Number")},
                {"difficulty_id",r.string(r.getter(row,"get_Id"))},{"produce_id",r.string(r.getter(row,"get_ProduceId"))},
                {"step_type",number(r,row,"get_StepType")}};
            auto button=index<cells.size()?r.getter(cells[index],"get_Button"):nullptr;
            const bool enabled=clickable(r,button);
            state["candidates"].push_back({{"identity",identity},{"enabled",enabled},
                {"selected",identity.at("number")==state.at("selected_number")},
                {"difficulty",json::parse(r.string(r.getter(row,"ToString")))}});
            if(state.at("remaining_count").get<int>()>0&&enabled&&identity.at("number")!=state.at("selected_number")){
                auto target=base;target.update(identity);target["button_instance_id"]=pointer_identity(button);
                snapshot["legal_actions"].push_back(action("audition.retry_select",target));
            }
        }
    }
    auto positive=execute(r,sheet);
    if(state.at("remaining_count").get<int>()>0&&clickable(r,positive)){
        auto target=base;target["selected_number"]=state.at("selected_number");target["button_instance_id"]=pointer_identity(positive);
        if(type=="ProduceAuditionContinueWarningSheetPresenter")snapshot["legal_actions"].push_back(action("audition.retry_open",target));
        else if(state.at("free_retry_available")==true)snapshot["legal_actions"].push_back(action("audition.retry_confirm_free",target));
        else if(state.at("ticket_retry_available")==true){
            target["item_id"]=state.at("ticket_item_id");target["item_balance"]=state.at("ticket_balance");target["max_cost"]=1;
            snapshot["legal_actions"].push_back(action("audition.retry_confirm_ticket",target));
        }
    }
    snapshot["ui_state"]=state;return true;
}
bool submit_audition_retry_action(Runtime& r,void* sheet,const json& target,const json& before){
    const auto id=target.at("action_id").get<std::string>();
    if(id!="audition.retry_open"&&id!="audition.retry_select"&&id!="audition.retry_confirm_free"&&id!="audition.retry_confirm_ticket")return false;
    if(before.at("screen_type")!=target.at("sheet_type")||pointer_identity(sheet)!=target.at("sheet_instance_id").get<std::string>())throw std::runtime_error("retry sheet changed");
    const auto current=quota(r);
    if(current.is_null()||current.at("remaining_count").get<int>()<=0||current.at("free_count")!=target.at("free_count")||
        current.at("remaining_count")!=target.at("remaining_count"))throw std::runtime_error("retry quota changed");
    if(id=="audition.retry_confirm_free"&&current.at("free_retry_available")!=true)throw std::runtime_error("free retry unavailable; no item substitution");
    if(id=="audition.retry_confirm_ticket"&&(current.at("ticket_retry_available")!=true||
        target.at("item_id")!="item-produce_continue-1"||target.at("item_id")!=current.at("ticket_item_id")||
        target.at("item_balance")!=current.at("ticket_balance")||target.at("price")!=1||target.at("max_cost")!=1||current.at("ticket_cost")!=1))
        throw std::runtime_error("single-ticket retry resource, balance or price changed");
    void* button{};
    if(id=="audition.retry_select"){
        auto model=r.getter(sheet,"get_Model");auto rows=r.enumerate(r.getter(model,"get_AuditionDifficultyList"),32);
        const auto index=target.at("index").get<std::size_t>();auto row=rows.at(index);
        if(number(r,row,"get_Number")!=target.at("number").get<int>()||r.string(r.getter(row,"get_Id"))!=target.at("difficulty_id").get<std::string>())throw std::runtime_error("retry tier changed");
        auto cells=r.enumerate(r.read_object_field(r.read_object_field(sheet,"_view"),"_selectAuditionlist"),32);
        button=r.getter(cells.at(index),"get_Button");
    }else button=execute(r,sheet);
    if(pointer_identity(button)!=target.at("button_instance_id").get<std::string>())throw std::runtime_error("retry button changed");
    click(r,button);return true;
}
}
