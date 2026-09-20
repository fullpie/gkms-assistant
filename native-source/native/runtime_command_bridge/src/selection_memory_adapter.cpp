#include "selection_memory_adapter.hpp"
#include "pointer_identity.hpp"
#include <set>

namespace gkms::bridge {
namespace {
int number(Runtime& r,void* o,const char* name){return r.unbox<int>(r.getter(o,name));}
bool flag(Runtime& r,void* o,const char* name){return r.unbox<bool>(r.getter(o,name));}
std::string text(Runtime& r,void* o,const char* name){return r.string(r.getter(o,name));}
json rows(Runtime& r,void* o,const char* name){
    json result=json::array();
    for(auto row:r.enumerate(r.getter(o,name),4096))result.push_back(json::parse(text(r,row,"ToString")));
    return result;
}
json project(Runtime& r,void* memory,bool detailed=false){
    if(!memory)return nullptr;
    json value={{"user_selection_memory_id",text(r,memory,"get_Id")},
        {"produce_id",text(r,memory,"get_ProduceId")},{"idol_card_id",text(r,memory,"get_IdolCardId")},
        {"character_id",text(r,memory,"get_CharacterId")},{"plan_type",number(r,memory,"get_PlanType")},
        {"grade",number(r,memory,"get_Grade")},{"star",number(r,memory,"get_Star")},
        {"stamina",number(r,memory,"get_Stamina")},{"vocal",number(r,memory,"get_Vocal")},
        {"dance",number(r,memory,"get_Dance")},{"visual",number(r,memory,"get_Visual")},
        {"vocal_growth_permil",number(r,memory,"get_VocalGrowthRatePermil")},
        {"dance_growth_permil",number(r,memory,"get_DanceGrowthRatePermil")},
        {"visual_growth_permil",number(r,memory,"get_VisualGrowthRatePermil")},
        {"cleared_time",r.unbox<std::int64_t>(r.getter(memory,"get_ClearedTime"))},
        {"cards",rows(r,memory,"get_ProduceCards")},{"items",rows(r,memory,"get_ProduceItems")},
        {"customize_items",rows(r,memory,"get_ProduceCustomizeItems")},{"detail_loaded",detailed},
        {"source",detailed?"native ProduceStartScreenModel.SelectionMemoryDetail":"native ProduceSelectInfo / current HIF selection-memory pool"}};
    if(value.at("user_selection_memory_id")=="")throw std::runtime_error("HIF selection memory has no actual identity");
    if(detailed){
        value["support_cards"]=rows(r,memory,"get_SupportCards");
        value["memories"]=rows(r,memory,"get_Memories");
        value["schedules"]=rows(r,memory,"get_Schedules");
    }
    return value;
}
void remove_continue(json& actions,const char* button_id){
    for(auto i=actions.begin();i!=actions.end();){
        if(i->at("action_id")=="ui.navigation"&&i->at("target").value("button_id",std::string())==button_id)i=actions.erase(i);
        else ++i;
    }
}
}

void append_selection_memory_prepare(Runtime& r,void* presenter,void* model,void* info,void* produce,
    const std::string& screen,json& selection,json& actions){
    const int split=number(r,produce,"get_ProduceSplitType");
    selection["produce_split_type"]=split;
    selection["split_pair_produce_id"]=text(r,produce,"get_SplitPairProduceId");
    selection["hif_inheritance_required"]=split==2;
    if(split!=2)return;
    const auto pair=selection.at("split_pair_produce_id").get<std::string>();
    const auto idol=selection.value("idol_card_id",json(nullptr));
    const auto idol_id=idol.is_string()?idol.get<std::string>():std::string();
    if(pair.empty())throw std::runtime_error("HIF final has no native paired selection produce");
    auto current=r.getter(info,"get_SelectionMemory");
    const auto current_memory=project(r,current);
    selection["selection_memory"]=current_memory;
    selection["hif_inheritance_ready"]=false;
    if(screen=="ProduceIdolSelectScreenPresenter"){
        if(!flag(r,model,"get_IsHifFinalRound"))throw std::runtime_error("HIF model and native split identity disagree");
        auto reactive=r.getter(model,"get_CurrentSelectionMemory");
        auto selected=reactive?r.getter(reactive,"get_Value"):nullptr;
        const auto selected_id=selected?text(r,selected,"get_Id"):std::string();
        const bool consistent=current_memory.is_object()&&current_memory.at("user_selection_memory_id")==selected_id;
        const bool bound=consistent&&selection_memory_matches_prepare(current_memory,pair,idol_id);
        selection["selected_selection_memory_id"]=selected_id;
        selection["selection_memory_identity_bound"]=bound;
        selection["selection_memories"]=json::array();
        auto pool=r.read_object_field(model,"_selectionMemories");
        selection["selection_memory_pool_ready"]=pool!=nullptr;
        if(pool){
            const auto memories=r.enumerate(pool,10000);
            const bool can_select=!flag(r,model,"get_IsTransitioning")&&!flag(r,model,"get_IsBlockSetCurrentCard");
            std::set<std::string> ids;
            for(std::size_t index=0;index<memories.size();++index){
                auto candidate=project(r,memories[index]);
                const auto id=candidate.at("user_selection_memory_id").get<std::string>();
                if(!ids.insert(id).second)throw std::runtime_error("HIF selection memory pool repeats identity");
                const bool eligible=selection_memory_matches_prepare(candidate,pair,idol_id);
                candidate["index"]=index;candidate["eligible"]=eligible;candidate["selected"]=id==selected_id;
                selection["selection_memories"].push_back(candidate);
                if(!can_select||!eligible||id==selected_id)continue;
                json target={{"action_id","produce.choose_selection_memory"},{"index",index},
                    {"user_selection_memory_id",id},{"source_produce_id",pair},
                    {"produce_id",selection.at("produce_id")},{"idol_card_id",idol_id},
                    {"model_instance_id",pointer_identity(model)}};
                actions.push_back({{"action_id","produce.choose_selection_memory"},{"target",target}});
            }
        }
        if(!bound)remove_continue(actions,"produce.idol_continue");
    }else if(screen=="ProduceStartScreenPresenter"){
        auto detail=r.invoke(r.method(r.object_class(model),"get_SelectionMemoryDetail",0,0x0600F28D),model);
        const auto detail_state=project(r,detail,true);
        const bool bound=selection_memory_matches_prepare(detail_state,pair,idol_id)&&current_memory.is_object()&&
            detail_state.at("user_selection_memory_id")==current_memory.at("user_selection_memory_id");
        selection["selection_memory_detail"]=detail_state;
        selection["hif_inheritance_ready"]=bound;
        if(!bound)remove_continue(actions,"produce.start");
    }
    (void)presenter;
}

bool submit_selection_memory_action(Runtime& r,void* presenter,const json& target,const json& before){
    if(target.value("action_id",std::string())!="produce.choose_selection_memory")return false;
    if(before.at("screen_type")!="ProduceIdolSelectScreenPresenter")throw std::runtime_error("selection memory requires current HIF idol preparation");
    auto model=r.read_object_field(presenter,"_model");
    if(!flag(r,model,"get_IsHifFinalRound")||flag(r,model,"get_IsTransitioning")||flag(r,model,"get_IsBlockSetCurrentCard")||
        pointer_identity(model)!=target.at("model_instance_id").get<std::string>())throw std::runtime_error("HIF selection-memory model is no longer ready");
    auto info=r.getter(model,"get_SelectInfo");auto produce=r.getter(info,"get_Produce");
    auto idol=r.getter(info,"get_UserIdolCard");
    if(number(r,produce,"get_ProduceSplitType")!=2||text(r,produce,"get_Id")!=target.at("produce_id").get<std::string>()||
        text(r,produce,"get_SplitPairProduceId")!=target.at("source_produce_id").get<std::string>()||!idol||text(r,idol,"get_CardId")!=target.at("idol_card_id").get<std::string>())
        throw std::runtime_error("HIF paired produce or current idol changed before selection");
    auto pool=r.enumerate(r.read_object_field(model,"_selectionMemories"),10000);
    auto memory=pool.at(target.at("index").get<std::size_t>());
    auto candidate=project(r,memory);
    if(candidate.at("user_selection_memory_id")!=target.at("user_selection_memory_id")||
        !selection_memory_matches_prepare(candidate,target.at("source_produce_id").get<std::string>(),target.at("idol_card_id").get<std::string>()))
        throw std::runtime_error("HIF selection-memory index identity changed");
    // Exact ISelectionMemory overload used by the game's list selection. It
    // updates SelectInfo, the reactive selection and the displayed card data.
    r.invoke(r.method(r.object_class(model),"SetCurrentSelectionMemory",1,0x0600EC57),model,{memory});
    return true;
}
}
