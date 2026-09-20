#include "audition_result_adapter.hpp"
#include "audition_retry_adapter.hpp"
#include "produce_lifecycle_adapter.hpp"
#include "pointer_identity.hpp"
#include <set>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* o,const char* name){return r.unbox<bool>(r.getter(o,name));}
int number(Runtime& r,void* o,const char* name){return r.unbox<int>(r.getter(o,name));}
bool active(Runtime& r,void* o){return o&&flag(r,r.getter(o,"get_gameObject"),"get_activeInHierarchy");}
void* next_button(Runtime& r,void* presenter){return r.getter(r.read_object_field(presenter,"_view"),"get_GoNextButton");}
json observed_button(Runtime& r,void* button){
    return {{"active",active(r,button)},{"enabled",button&&flag(r,button,"get_IsEnabled")},
        {"disabled",!button||flag(r,button,"get_IsDisabled")},
        {"has_callback",button&&r.read_object_field(button,"onClickedCallback")!=nullptr}};
}
bool actionable(const json& button){return button.at("active")==true&&button.at("enabled")==true&&button.at("disabled")==false&&button.at("has_callback")==true;}
json result_state(Runtime& r,void* presenter){
    auto model=r.read_object_field(presenter,"_model");
    if(!model)return {{"data_ready",false}};
    const bool win=flag(r,model,"get_IsWin"),complete=flag(r,model,"get_IsComplete"),has_item=flag(r,model,"get_HasContinueItem");
    const int remaining=number(r,model,"get_RemainContinueCount"),free=number(r,model,"get_RemainFreeContinueCount");
    const int score=number(r,model,"get_Score");
    const auto context=read_produce_context(r);
    json state={{"data_ready",true},{"is_win",win},{"is_complete",complete},{"score",score},
        {"ranking_border",number(r,model,"get_RankingBorder")},{"remaining_count",remaining},{"free_count",free},
        {"has_continue_item",has_item},{"can_retry",remaining>0&&(free>0||has_item)},
        {"step_type",number(r,model,"get_StepType")},{"selected_number",context.is_object()?context.at("audition_number"):json(nullptr)},
        {"produce_context",context},{"score_rows",json::array()},{"rank",nullptr},
        {"rank_source","unavailable until one current non-NPC score row matches the model score"},
        {"source","current AuditionBattleResultScreenModel, current schedule and result score rows"}};
    std::set<int> npc_numbers;
    if(auto npcs=r.getter(model,"get_NpcDataList"))for(auto npc:r.enumerate(npcs,128))npc_numbers.insert(number(r,npc,"get_Number"));
    int matched=0;json rank=nullptr;
    if(auto rows=r.getter(model,"get_AfterAuditionScoreDataList"))for(auto row:r.enumerate(rows,128)){
        const int identity=number(r,row,"get_Number"),point=number(r,row,"get_Point"),value=number(r,row,"get_Rank");
        state["score_rows"].push_back({{"number",identity},{"point",point},{"rank",value},{"is_npc",npc_numbers.contains(identity)}});
        if(!npc_numbers.contains(identity)&&point==score&&value>0){++matched;rank=value;}
    }
    if(matched==1){state["rank"]=rank;state["rank_source"]="unique non-NPC AfterAuditionScoreDataList row matching current Score";}
    auto button=next_button(r,presenter);state["next_button"]=observed_button(r,button);
    return state;
}
json target_for(Runtime& r,void* presenter,void* button,const json& state,const char* id){
    return {{"action_id",id},{"owner_instance_id",pointer_identity(presenter)},{"button_instance_id",pointer_identity(button)},
        {"callback_instance_id",pointer_identity(r.read_object_field(button,"onClickedCallback"))},
        {"is_win",state.at("is_win")},{"is_complete",state.at("is_complete")},{"score",state.at("score")},
        {"ranking_border",state.at("ranking_border")},{"remaining_count",state.at("remaining_count")},
        {"free_count",state.at("free_count")},{"has_continue_item",state.at("has_continue_item")},
        {"selected_number",state.at("selected_number")},{"step_type",state.at("step_type")}};
}
}
bool append_audition_result_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(!native_audition_result_parent(screen))return false;
    // Replace the old untyped GoNext navigation before any host decision.
    snapshot["surface"]="audition_result";snapshot["legal_actions"]=json::array();
    const auto state=result_state(r,presenter);snapshot["ui_state"]=state;
    snapshot["actions_complete"]=state.value("data_ready",false)&&snapshot.at("blockers").empty();
    if(state.value("data_ready",false)){
        const auto* id=audition_result_action_id(state.at("is_complete").get<bool>(),state.at("is_win").get<bool>(),
            state.at("remaining_count").get<int>(),state.at("free_count").get<int>(),state.at("has_continue_item").get<bool>());
        if(id&&actionable(state.at("next_button"))){
            auto target=target_for(r,presenter,next_button(r,presenter),state,id);
            snapshot["legal_actions"].push_back({{"action_id",id},{"target",target}});
        }
    }
    return true;
}
bool submit_audition_result_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());
    if(id!="audition.continue_after_win"&&id!="audition.open_retry_result"&&id!="audition.finish_failed")return false;
    if(!native_audition_result_parent(before.at("screen_type").get<std::string>()))throw std::runtime_error("audition result owner changed");
    const auto state=result_state(r,presenter);
    if(!state.value("data_ready",false))throw std::runtime_error("audition result state is unavailable");
    const auto* current=audition_result_action_id(state.at("is_complete").get<bool>(),state.at("is_win").get<bool>(),
        state.at("remaining_count").get<int>(),state.at("free_count").get<int>(),state.at("has_continue_item").get<bool>());
    auto button=next_button(r,presenter);
    if(!current||id!=current||!actionable(state.at("next_button"))||target_for(r,presenter,button,state,current)!=target)
        throw std::runtime_error("audition outcome/quota or next-button identity/readiness changed");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);return true;
}
}
