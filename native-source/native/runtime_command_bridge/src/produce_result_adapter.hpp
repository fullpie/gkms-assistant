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
bool append_produce_result_actions(Runtime&,void*,const std::string&,json&);
bool submit_produce_result_action(Runtime&,void*,const json&,const json&);
}
