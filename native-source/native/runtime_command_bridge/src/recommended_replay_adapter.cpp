#include "recommended_replay_adapter.hpp"
#include "pointer_identity.hpp"
#include <algorithm>

namespace gkms::bridge {
namespace {
constexpr const char* screen_name="ProducerRankingRecommendReplayScreenPresenter";
bool flag(Runtime& r,void* object,const char* name){return r.unbox<bool>(r.getter(object,name));}
int number(Runtime& r,void* object,const char* name){return r.unbox<int>(r.getter(object,name));}
std::string text(Runtime& r,void* object,const char* name){return r.string(r.getter(object,name));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
void expect(Runtime& r,void* object,const char* type){
    if(!object||r.class_name(r.object_class(object))!=type)throw std::runtime_error(std::string("replay UI type changed: ")+type);
}
json proto(Runtime& r,void* object){return object?json::parse(text(r,object,"ToString")):json(nullptr);}
std::vector<void*> sequence(Runtime& r,void* collection,std::size_t maximum){
    if(!collection)throw std::runtime_error("replay sequence collection is unavailable");
    for(auto klass=r.object_class(collection);klass;klass=r.parent(klass)){
        const auto name=r.class_name(klass);
        if(name=="List`1"||name=="RepeatedField`1"||name=="ReadOnlyCollection`1"||name=="Array")
            return r.enumerate(collection,maximum);
    }
    // In particular, Dictionary.get_Item(int) accepts a key, not a row.
    throw std::runtime_error("unsupported replay sequence collection: "+r.class_name(r.object_class(collection)));
}
void* exact_getter(Runtime& r,void* object,const char* name,std::uint32_t token){return r.invoke(r.method(r.object_class(object),name,0,token),object);}
struct Delegate {void* instance{};void* target{};int token{};std::uint32_t verified_token{};};
Delegate single_delegate(Runtime& r,void* callback,const json& source){
    if(!callback)return {};
    const auto delegates=sequence(r,r.getter(callback,"GetInvocationList"),16);
    if(delegates.size()!=1)return {};
    auto method=r.getter(delegates.front(),"get_Method");
    return {callback,r.getter(delegates.front(),"get_Target"),number(r,method,"get_MetadataToken"),
        read_profiled_callback_token(r,method,source)};
}
json blocking(Runtime& r){
    int content=0,dialog=0;
    auto klass=r.klass("Assembly-CSharp","Campus.Common","CampusBlockingManager");
    if(r.unbox<bool>(r.invoke(r.method(klass,"get_ExistInstance",0,0x06000093),nullptr))){
        auto manager=r.invoke(r.method(klass,"get_Instance",0,0x06000092),nullptr);
        auto counters=r.read_object_field(manager,"_blockingCounter");
        auto lookup=r.method(r.object_class(counters),"TryGetValue",2);
        int order=13;r.invoke(lookup,counters,{&order,&content});
        order=17;r.invoke(lookup,counters,{&order,&dialog});
    }
    return {{"content_blocking_count",content},{"dialog_blocking_count",dialog}};
}
bool pointer_ready(Runtime& r,void* button){
    if(!active(r,button)||!flag(r,button,"get_IsEnabled")||flag(r,button,"get_IsDisabled"))return false;
    auto type=r.reflection_type(r.klass("UnityEngine.UIModule","UnityEngine","CanvasGroup"));
    // Exact PC non-generic Component.GetComponentsInParent(Type), not the
    // generic overload whose sole argument is includeInactive.
    auto groups=r.invoke(r.method(r.object_class(button),"GetComponentsInParent",1,0x060013CF),button,{type});
    float alpha=1.f;
    for(auto group:sequence(r,groups,64)){
        alpha*=r.unbox<float>(r.getter(group,"get_alpha"));
        if(!flag(r,group,"get_interactable")||!flag(r,group,"get_blocksRaycasts")||alpha<=.001f)return false;
        if(flag(r,group,"get_ignoreParentGroups"))break;
    }
    return true;
}
struct Candidate {json action;void* button;};
struct Projection {json state;std::vector<Candidate> candidates;};
Projection project(Runtime& r,void* presenter,const json& before){
    expect(r,presenter,screen_name);
    auto param=r.read_object_field(presenter,"_param");
    expect(r,param,"ProducerRankingRecommendReplayScreenTransitionParam");
    auto list=r.read_object_field(presenter,"_listPresenter");
    expect(r,list,"ProducerRankingRecommendReplayListPresenter");
    Projection result;
    auto& state=result.state;
    state={{"produce_id",text(r,param,"get_ProduceId")},{"idol_card_id",text(r,param,"get_IdolCardId")},
        {"producer_level_min",r.field<int>(presenter,"_currentLevelMin")},
        {"producer_level_max",r.field<int>(presenter,"_currentLevelMax")},
        {"is_high_score_rush",flag(r,param,"get_IsHighScoreRush")},{"is_research",flag(r,param,"get_IsResearch")},
        {"histories",json::array()},{"source","current-native-recommendation-item-models"},
        {"blocking",blocking(r)}};
    const bool produce_active=before.contains("state")&&before.at("state").is_object()&&before.at("state").value("in_progress",true);
    const bool scope=recommended_replay_scope(screen_name,state,produce_active);
    const bool blocked=state.at("blocking").at("content_blocking_count").get<int>()>0||
        state.at("blocking").at("dialog_blocking_count").get<int>()>0;
    state["scope_ready"]=scope;
    auto models=sequence(r,r.getter(list,"get_ItemModels"),256);
    for(int index=0;index<static_cast<int>(models.size());++index){
        auto item=models.at(index);expect(r,item,"ProducerRankingRecommendReplayListItemModel");
        auto history=exact_getter(r,item,"get_ProduceHistory",0x0600F8A8);
        auto source=proto(r,r.read_object_field(item,"_data"));
        auto history_json=proto(r,history);
        if(!source.is_object()||!source.contains("produceHistory")||source.at("produceHistory")!=history_json)
            throw std::runtime_error("replay item and original history disagree");
        const auto history_digest=sha256(history_json.dump()),source_digest=sha256(source.dump());
        auto cell=r.invoke(r.method(r.object_class(list),"GetCellFromIndex",1),list,{&index});
        const bool realized=active(r,cell);
        json row={{"history_digest",history_digest},{"source_history_digest",source_digest},{"source_history",source},
            {"user_memory_id",text(r,item,"get_UserMemoryId")},{"item_instance_id",pointer_identity(item)},
            {"cell_instance_id",pointer_identity(cell)},{"realized",realized},{"auditions",json::array()}};
        if(!realized){state["histories"].push_back(std::move(row));continue;}
        expect(r,cell,"ProducerRankingRecommendReplayListCellPresenter");
        if(r.getter(cell,"GetItemModel")!=item)throw std::runtime_error("replay list cell was recycled");
        auto view=exact_getter(r,cell,"GetView",0x0600F894);expect(r,view,"ProducerRankingRecommendReplayListCellView");
        const auto auditions=sequence(r,exact_getter(r,history,"get_Auditions",0x0602149C),64);
        const auto score_rows=sequence(r,r.read_object_field(view,"_auditionScoreRows"),64);
        for(auto score_row:score_rows){
            expect(r,score_row,"ProducerRankingRecommendReplayAuditionScoreRow");
            auto callback=single_delegate(r,exact_getter(r,score_row,"get_ReplayCallback",0x0600F885),recommended_replay_callback_source(false));
            json observed={{"row_instance_id",pointer_identity(score_row)},{"input_ready",false},
                {"replay_callback_method_token",callback.token}};
            if(!callback.verified_token||!callback.target){
                observed["unavailable_reason"]="audition-delegate-contract-unverified";row["auditions"].push_back(std::move(observed));continue;
            }
            auto audition=r.read_object_field(callback.target,"audition");
            if(r.read_object_field(callback.target,"<>4__this")!=view||std::count(auditions.begin(),auditions.end(),audition)!=1)
                throw std::runtime_error("replay callback audition does not belong to its visible history");
            auto button=exact_getter(r,score_row,"get_ReplayButton",0x0600F884);
            auto button_callback=single_delegate(r,button?r.read_object_field(button,"onClickedCallback"):nullptr,recommended_replay_callback_source(true));
            const int step=r.unbox<int>(exact_getter(r,audition,"get_StepType",0x0602151F));
            const int selection=r.unbox<int>(exact_getter(r,audition,"get_StepSelectNumber",0x06021521));
            const bool has_actions=recommended_replay_has_actions(proto(r,audition));
            const bool source_matches=history_json.value("produceId",std::string())==state.at("produce_id").get<std::string>()&&
                history_json.value("idolCardId",std::string())==state.at("idol_card_id").get<std::string>();
            const bool callback_matches=recommended_replay_callback_matches(callback.token,callback.verified_token,
                r.read_object_field(callback.target,"<>4__this")==view,
                static_cast<int>(std::count(auditions.begin(),auditions.end(),audition)),button_callback.token,button_callback.verified_token,button_callback.target==score_row);
            const bool ready=scope&&!blocked&&step>=16&&step<=18&&active(r,presenter)&&active(r,score_row)&&source_matches&&has_actions&&
                !flag(r,item,"get_IsBlocked")&&callback_matches&&pointer_ready(r,button);
            observed.update({{"step_type",step},{"step_select_number",selection},{"rank",number(r,audition,"get_Rank")},
                {"score",number(r,audition,"get_Score")},{"audition_instance_id",pointer_identity(audition)},
                {"button_instance_id",pointer_identity(button)},{"button_callback_method_token",button_callback.token},
                {"callback_contract_ready",callback_matches},{"has_replay_actions",has_actions},{"input_ready",ready}});
            row["auditions"].push_back(observed);
            json target={{"page_instance_id",pointer_identity(presenter)},{"list_instance_id",pointer_identity(list)},
                {"item_instance_id",pointer_identity(item)},{"cell_instance_id",pointer_identity(cell)},{"view_instance_id",pointer_identity(view)},
                {"row_instance_id",pointer_identity(score_row)},{"button_instance_id",pointer_identity(button)},
                {"button_callback_instance_id",pointer_identity(button_callback.instance)},
                {"replay_callback_instance_id",pointer_identity(callback.instance)},{"audition_instance_id",pointer_identity(audition)},
                {"history_instance_id",pointer_identity(history)},{"history_digest",history_digest},{"source_history_digest",source_digest},
                {"user_memory_id",row.at("user_memory_id")},{"step_type",step},{"step_select_number",selection},
                {"produce_id",state.at("produce_id")},{"idol_card_id",state.at("idol_card_id")},
                {"producer_level_min",state.at("producer_level_min")},{"producer_level_max",state.at("producer_level_max")}};
            auto action=recommended_replay_action(target,ready);
            if(!action.is_null())result.candidates.push_back({std::move(action),button});
        }
        state["histories"].push_back(std::move(row));
    }
    return result;
}
}
bool append_recommended_replay_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen!=screen_name)return false;
    auto projection=project(r,presenter,snapshot);
    snapshot["surface"]="recommended_replay";snapshot["ui_state"]=std::move(projection.state);
    snapshot["legal_actions"]=json::array();
    for(auto& candidate:projection.candidates)snapshot["legal_actions"].push_back(std::move(candidate.action));
    snapshot["actions_complete"]=snapshot.at("blockers").empty();return true;
}
bool submit_recommended_replay_action(Runtime& r,void* presenter,const json& target,const json& before){
    if(target.value("action_id",std::string())!="replay.start")return false;
    if(before.at("screen_type")!=screen_name)throw std::runtime_error("replay recommendation page changed");
    auto projection=project(r,presenter,before);void* button{};
    for(const auto& candidate:projection.candidates)if(candidate.action.at("target")==target){
        if(button)throw std::runtime_error("replay start target is ambiguous");button=candidate.button;
    }
    if(!button)throw std::runtime_error("replay row identity or pointer readiness changed");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);return true;
}
}
