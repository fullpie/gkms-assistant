#include "exam_model_observation.hpp"
#include "exam_save_serializer.hpp"
#include "pointer_identity.hpp"
#include "pc_method_binding.hpp"
#include "exam_reference_presence.hpp"
#include "exam_model_revision.hpp"
#include <set>

namespace gkms::bridge {
namespace {
void* observed_getter(Runtime& r,void* object,const char* name,std::uint32_t token){
    return r.invoke(r.method(r.object_class(object),name,0,token),object);
}
json execution_master(Runtime& r){
    // Reuse the established official-replay observer's existing-instance path.
    // This never calls the singleton getter or initializes/swaps any Master.
    json result={{"schema","gkms.native-execution-master-observation.v1"},
        {"execution_master_version",nullptr},{"execution_master_hash",nullptr},
        {"authority","native-existing-MasterManager"},{"manager_instance_id",nullptr},{"ready",false}};
    try{
        auto klass=r.klass("Assembly-CSharp","Campus.Common.Master","MasterManager");
        auto unity=r.klass("UnityEngine.CoreModule","UnityEngine","Object");
        auto found=r.invoke(r.method(unity,"FindObjectsOfType",1,0x06001546),nullptr,{r.reflection_type(klass)});
        auto managers=r.enumerate(found,4);result["manager_count"]=managers.size();
        if(managers.size()!=1||!managers[0]||r.object_class(managers[0])!=klass)
            throw std::runtime_error("one existing MasterManager required");
        auto manager=managers[0];const auto version=r.string(r.read_object_field(manager,"_masterVersion"));
        const bool updated=r.field<bool>(manager,"_isSuccessMasterUpdate"),initialized=r.field<bool>(manager,"_isInitializeMaster");
        result.update({{"execution_master_version",version},{"manager_instance_id",pointer_identity(manager)},
            {"master_update_succeeded",updated},{"master_tables_initialized",initialized},
            {"ready",!version.empty()&&updated&&initialized}});
    }catch(const std::exception& error){result["error"]=error.what();}
    return result;
}
json base(Runtime& r,void* sequence,void* parameter,const char* kind,const json& before){
    if(!r.managed_thread()||!r.operation_active())throw std::runtime_error("model observation requires managed owner");
    return {{"schema","gkms.live-exam-model-observation.v1"},{"decision_type",kind},
        {"sequence_id",pointer_identity(sequence)},{"parameter_id",pointer_identity(parameter)},
        {"engine_identity",verified_pc_binding_identity()},
        {"execution_master",execution_master(r)},{"state_before",before},{"read_errors",json::array()},
        {"serialization_source","original ExamSaveData(sequence,false) and JsonUtility.ToJson(object,false)"},
        {"input_authority",false},{"training_qualified",false},{"model_compatibility_qualified",false}};
}
void finish(Runtime& r,ExamSaveSerializer& serializer,void* sequence,void* parameter,json& result,bool owner_stable){
    json presence_after;
    const auto after=serializer.capture_with_presence(sequence,parameter,presence_after);
    const auto after_master=execution_master(r);
    const auto count_after=observe_total_effect_draw_count(r,parameter);
    result["reference_presence_after"]=presence_after;
    result["save_object_id_after"]=presence_after.at("save_object_id");
    result["live_counters"]["totalDrawCardCount"]["after"]=count_after;
    const bool counter_stable=result.at("live_counters").at("totalDrawCardCount").at("before")==count_after;
    if(presence_after.at("complete")!=true)result["read_errors"].push_back("after-reference-presence-incomplete");
    const bool presence_stable=reference_presence_revision_view(result.at("reference_presence"))==
        reference_presence_revision_view(presence_after);
    if(!presence_stable)result["read_errors"].push_back("source-reference-presence-changed-during-observation");
    const bool master_stable=result.at("execution_master")==after_master;
    owner_stable=owner_stable&&r.getter(sequence,"get_Parameter")==parameter&&master_stable;
    result["state_after"]=after;
    result["purity"]={{"state_equal",result.at("state_before")==after},{"owner_stable",owner_stable},
        {"before_native_sha256",sha256(result.at("state_before").dump())},{"after_native_sha256",sha256(after.dump())},
        {"hash_encoding","nlohmann-json-dump"},{"execution_master_stable",master_stable},
        {"live_counter_stable",counter_stable},{"reference_presence_stable",presence_stable}};
    result["complete"]=result.at("read_errors").empty()&&result.at("purity").at("state_equal")==true&&owner_stable&&counter_stable;
}
void begin_feature_evidence(void* parameter,json& result,const json& presence,int counter){
    if(presence.at("state_native_sha256")!=sha256(result.at("state_before").dump()))
        result["read_errors"].push_back("reference-presence-save-differs-from-original-observation");
    result["reference_presence"]=presence;
    result["save_object_id"]=presence.at("save_object_id");
    result["live_counters"]={{"totalDrawCardCount",{{"source","ExamParameterModel.TotalEffectDrawCardCount"},
        {"parameter_id",pointer_identity(parameter)},{"value",counter},{"before",counter},{"after",nullptr}}}};
    if(presence.at("complete")!=true)result["read_errors"].push_back("before-reference-presence-incomplete");
}
std::vector<void*> ui_cards(Runtime& r,void* selector,std::vector<void*>& models){
    auto list=r.read_object_field(selector,"_cardList");models=r.enumerate(r.getter(list,"get_ItemModels"),4096);
    std::vector<void*> cards;for(auto model:models)cards.push_back(r.getter(model,"get_Card"));return cards;
}
}

json read_live_model_context(Runtime& r){
    if(!r.managed_thread()||!r.operation_active())throw std::runtime_error("model context requires managed owner");
    return {{"schema","gkms.live-exam-model-preflight.v1"},
        {"engine_identity",verified_pc_binding_identity()},
        {"execution_master",execution_master(r)},{"input_authority",false}};
}

json capture_main_model_observation(Runtime& r,void* sequence,void* parameter,const json& before,const json& legal,
    const json& presence,int draw_count_before){
    auto result=base(r,sequence,parameter,"main",before);result["native_legal_inputs"]=legal;
    ExamSaveSerializer serializer;serializer.initialize(r);
    begin_feature_evidence(parameter,result,presence,draw_count_before);
    finish(r,serializer,sequence,parameter,result,true);
    if(!legal.value("complete",false)||!legal.value("boundary_ready",false))result["complete"]=false;
    return result;
}

json capture_secondary_model_observation(Runtime& r,void* sequence,void* parameter,void* selector,
    const ExamCloseSelectorOwner& owner,const json& parent_context,const json& ui_state){
    ExamSaveSerializer serializer;serializer.initialize(r);
    const auto draw_count_before=observe_total_effect_draw_count(r,parameter);
    json presence;const auto before=serializer.capture_with_presence(sequence,parameter,presence);
    auto result=base(r,sequence,parameter,"secondary",before);
    begin_feature_evidence(parameter,result,presence,draw_count_before);
    result["parent_context"]=parent_context;result["ui_pool"]=ui_state.at("candidates");
    result["candidate_cards"]=json::array();result["selected_ui_indices"]=json::array();
    auto handler=r.read_boxed_field(owner.handler_runner,"stateMachine");
    auto context=r.read_object_field(handler,"context");
    if(r.read_object_field(handler,"command")!=owner.command||
        r.read_object_field(context,"<ExamParameter>k__BackingField")!=parameter||
        r.read_object_field(context,"<CommandExecutor>k__BackingField")!=sequence||r.field<bool>(context,"_disposed"))
        throw std::runtime_error("model DTO lost its actual selector context");
    result["context_binding"]={{"command_id",pointer_identity(owner.command)},{"effect_context_id",pointer_identity(context)},
        {"parameter_id",pointer_identity(parameter)},{"sequence_id",pointer_identity(sequence)},
        {"handler_runner_id",pointer_identity(owner.handler_runner)},{"disposed",false},
        {"source","actual pending OnEffectSelectParameterAsync state-machine fields; close-owner chain verified"}};
    // Sample original command pointers before JsonUtility can materialize
    // optional fields. The known command source fields are all represented.
    const json command_fields={{"_playingCard",nullptr},{"_playingDrink",nullptr},
        {"_playingItem",nullptr},{"_playingGimmick",nullptr}};
    const auto command_presence_before=observe_command_reference_presence(r,owner.command,command_fields);
    result["command"]=serializer.capture_object(owner.command);
    result["command_native_sha256"]=sha256(result.at("command").dump());
    result["command_presence"]=observe_command_reference_presence(r,owner.command,result.at("command"));
    if(command_presence_before.at("complete")!=true||
        command_presence_before.at("fields")!=result.at("command_presence").at("fields")||
        command_presence_before.at("card_phase_counters")!=result.at("command_presence").at("card_phase_counters"))
        result["read_errors"].push_back("command-reference-presence-changed-during-first-serialization");
    if(result.at("command_presence").at("complete")!=true)result["read_errors"].push_back("command-reference-presence-incomplete");
    const auto candidates=r.enumerate(r.read_object_field(handler,"candidateCardList"),4096);
    const int low=r.field<int>(handler,"pickMin"),high=r.field<int>(handler,"pickMax");
    result["constraints"]={{"pick_min",low},{"pick_max",high},{"offered_count",candidates.size()},
        {"is_hand",nullptr},{"is_hand_source","original argument not retained in this handler state machine"}};
    // Original GetSearchCardList's fourth result: effect98 first branch is
    // Hand; otherwise the actual search object's typed CardPositionType==2.
    // Both selectors flagged is ambiguous at this suspended handler: do not
    // invent which branch is active or rerun a pool-building utility.
    const bool first=r.field<bool>(owner.command,"_isCardSelect");
    const bool second=r.field<bool>(owner.command,"_isCardSelect2");
    if(first&&!second){
        auto effect=r.read_object_field(owner.command,"_playEffect");
        if(!effect)throw std::runtime_error("selector effect is absent");
        const auto effect_type=r.unbox<int>(r.getter(effect,"get_EffectType"));
        auto search=observed_getter(r,owner.command,"get_CardSelectSearch",0x06004D9C);
        const bool hand=effect_type==98||(search&&r.unbox<int>(r.getter(search,"get_CardPositionType"))==2);
        result["constraints"]["is_hand"]=hand;
        result["constraints"]["is_hand_source"]="original-PC GetSearchCardList fourth-item expression; first-only branch";
        result["selector_branch"]="first";
    }else{
        result["selector_branch"]="unqualified";
        result["read_errors"].push_back("secondary-selector-branch-not-unambiguously-bound");
    }
    std::vector<void*> models;const auto cards=ui_cards(r,selector,models);std::set<std::size_t> bound_ui;
    if(cards.size()!=candidates.size())result["read_errors"].push_back("native-offered-and-UI-model-count-differ");
    for(std::size_t i=0;i<candidates.size();++i){
        auto position=candidates[i];if(!position||r.class_name(r.object_class(position))!="ExamCardPositionData")
            throw std::runtime_error("actual secondary candidate position type differs");
        auto card=observed_getter(r,position,"get_CardData",0x0600642E);
        if(!card)throw std::runtime_error("null actual secondary candidate card");
        auto card_data=r.has_field(r.object_class(card),"_cardData")?r.read_object_field(card,"_cardData"):nullptr;
        std::vector<std::size_t> matches;
        for(std::size_t j=0;j<cards.size();++j)if(cards[j]==card||(card_data&&cards[j]==card_data))matches.push_back(j);
        json row={{"native_ordinal",i},{"native_object_id",pointer_identity(card)},{"card",serializer.capture_object(card)},
            {"position_object_id",pointer_identity(position)},
            {"native_zone_index",r.unbox<int>(observed_getter(r,position,"get_Index",0x0600642D))},
            {"native_zone_type",r.unbox<int>(observed_getter(r,position,"get_CardPositionType",0x0600642C))},
            {"ui_index",nullptr},{"ui_binding_source",nullptr}};
        if(matches.size()==1&&bound_ui.insert(matches[0]).second){
            row["ui_index"]=matches[0];row["ui_binding_source"]=cards[matches[0]]==card?"same-native-card-object":"same-observed-cardData-object";
        }else result["read_errors"].push_back("native-candidate-has-no-unique-pointer-bound-UI-model:"+std::to_string(i));
        result["candidate_cards"].push_back(std::move(row));
    }
    for(std::size_t i=0;i<models.size();++i)
        result["ui_pool"].at(i)["select_order"]=r.field<int>(models[i],"<SelectOrder>k__BackingField");
    auto indices=r.invoke(r.method(r.object_class(selector),"GetSelectIndex",0,0x06005E75),selector);
    for(auto boxed:r.enumerate(indices,4096))result["selected_ui_indices"].push_back(r.unbox<int>(boxed));
    result["selected_index_source"]="original ProduceCardSelectorOverlayPresenter.GetSelectIndex 06005e75";
    const auto again=read_exam_close_selector_owner(r,selector,r.read_object_field(handler,"<>4__this"),sequence,parameter,low,high);
    const bool same_owner=again.command==owner.command&&again.handler_runner==owner.handler_runner&&again.completion==owner.completion;
    const auto command_after=serializer.capture_object(owner.command);
    result["command_after"]=command_after;
    result["command_native_sha256_after"]=sha256(command_after.dump());
    result["command_presence_after"]=observe_command_reference_presence(r,owner.command,command_after);
    if(command_after!=result.at("command")||result.at("command_presence_after").at("complete")!=true||
        result.at("command_presence")!=result.at("command_presence_after"))
        result["read_errors"].push_back("command-reference-presence-not-stable");
    finish(r,serializer,sequence,parameter,result,same_owner);
    return result;
}
}
