#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
inline bool result_photo_selection_allowed(bool visible,const std::string& created_memory_id,
    bool has_selected_photo=false,bool selection_complete_ready=true){
    // OnSelectMemoryAsync disables this control before opening the confirm
    // sheet, through upload/ProduceEndAsync. The still-visible photo list
    // must not re-enable it by receiving another selection callback.
    return visible&&created_memory_id.empty()&&(!has_selected_photo||selection_complete_ready);
}
inline const char* result_memory_confirmation_action(const std::string& assigned_memory_id){
    return assigned_memory_id.empty()?"result.confirm_create_memory":"result.cancel_create_memory";
}
inline bool result_memory_confirmation_ready(const std::string& screen,bool active,bool closing,
    bool disable_interaction,bool root_canvas_interactive){
    // These interaction properties belong to SheetPresenterBase, not every
    // overlay. Keep their use scoped to this proven memory-confirmation type.
    return screen=="MemoryCreateConfirmSheetPresenter"&&active&&!closing&&!disable_interaction&&root_canvas_interactive;
}
inline json result_created_memory_value(const std::string& assigned,const std::string& observed,
    const std::string& idol,const std::string& produce,std::optional<int> grade){
    if(assigned.empty()||observed!=assigned||idol.empty()||produce.empty())return nullptr;
    return {{"memory_id",observed},{"idol_card_id",idol},{"produce_id",produce},
        {"grade",grade&&*grade>0?json(*grade):json(nullptr)},{"source","UserMemory.Grade"}};
}
inline bool result_memory_collection_getter_valid(const json& result,const json& parameters){
    return result.value("is_static",false)&&
        result.value("return_type",std::string())=="Campus.Common.User.UserMemoryCollection"&&
        parameters.value("parameter_count",-1)==0;
}
template<class Reader> void append_created_memory_presentation(json& state,Reader&& reader){
    const auto assigned=state.value("created_memory_id",std::string());
    if(assigned.empty()){
        state["created_memory_result_status"]={{"status","not-assigned"}};
        return;
    }
    // This catch is limited to the optional read-only presentation query. The
    // existing result ownership, button and transaction errors still propagate.
    try{
        auto result=reader(assigned);
        if(result.is_null()){
            state["created_memory_result_status"]={{"status","not-in-user-memory-list"}};
            return;
        }
        if(!result.is_object()||result.value("memory_id",std::string())!=assigned||
            result.value("source",std::string())!="UserMemory.Grade")
            throw std::runtime_error("created memory presentation identity differs");
        const bool known=result.contains("grade")&&!result.at("grade").is_null();
        state["created_memory_result"]=std::move(result);
        state["created_memory_result_status"]={{"status",known?"available":"grade-unavailable"}};
    }catch(const std::exception& error){
        state["created_memory_result_status"]={{"status","unavailable"},{"reason",error.what()}};
    }
}
bool append_produce_result_actions(Runtime&,void*,const std::string&,json&);
bool submit_produce_result_action(Runtime&,void*,const json&,const json&);
}
