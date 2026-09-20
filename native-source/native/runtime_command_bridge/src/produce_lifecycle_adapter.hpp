#pragma once
#include "runtime.hpp"
#include "callback_method_contract.hpp"

namespace gkms::bridge {
inline json produce_retire_callback_source(const std::string& screen){
    if(screen=="HomeProduceProgressSheetPresenter")return callback_method_source("Assembly-CSharp.dll","Campus.OutGame",
        {"HomeProduceProgressSheetPresenter"},"<SetEvent>b__1_0",0,0x0600AABC);
    if(screen=="ProduceRetireSheetPresenter")return callback_method_source("Assembly-CSharp.dll","Campus.Common",
        {"SheetPresenterBase`2"},"OnClickedExecuteButton",0,0x060161B2);
    return nullptr;
}
inline bool produce_retire_callback_matches(const std::string& screen,int token,std::uint32_t verified_token,bool owner_matches){
    return owner_matches&&verified_token&&token>0&&static_cast<std::uint32_t>(token)==verified_token&&
        (screen=="HomeProduceProgressSheetPresenter"||screen=="ProduceRetireSheetPresenter");
}
inline json produce_retire_manual_action(const std::string& screen,json target,const json& observed){
    const bool supported=screen=="HomeProduceProgressSheetPresenter"||screen=="ProduceRetireSheetPresenter";
    if(!supported||!observed.value("in_progress",false)||!observed.value("active",false)||
        !observed.value("enabled",false)||observed.value("disabled",true)||observed.value("closing",true)||
        !observed.value("callback_contract_ready",false)||!observed.value("pointer_ready",false)||
        !observed.value("origin_ready",false)||target.value("progress_digest",std::string()).empty())return nullptr;
    const auto id=screen=="HomeProduceProgressSheetPresenter"?"produce.retire.open":"produce.retire.confirm";
    target["action_id"]=id;target["manual_only"]=true;
    return {{"action_id",id},{"manual_only",true},{"target",std::move(target)}};
}
bool append_produce_lifecycle_actions(Runtime& runtime,void* presenter,
                                     const std::string& screen,json& snapshot);
bool submit_produce_lifecycle_action(Runtime& runtime,void* presenter,
                                    const json& target,const json& before);
json read_produce_context(Runtime& runtime);
void append_effect_confirmation(Runtime& runtime,void* presenter,const std::string& screen,json& snapshot);
}
