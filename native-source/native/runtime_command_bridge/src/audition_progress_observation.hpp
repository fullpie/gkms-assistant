#pragma once
#include "runtime.hpp"
#include <array>
#include <string_view>

namespace gkms::bridge {
struct AuditionObservationGetter {
    const char* name;
    const char* field;
    const char* return_type;
    std::uint32_t original_token;
    std::uint32_t updated_token;
};
inline constexpr std::array<AuditionObservationGetter,7> audition_observation_getters={{{
    "get_Status","status","Campus.Common.Proto.Client.Enums.ProduceProgressAuditionStatusType",0x0601ACF9,0x0601ADBF},
    {"get_VocalPermil","vocal_base_permil","System.Int32",0x0601ACFB,0x0601ADC1},
    {"get_DancePermil","dance_base_permil","System.Int32",0x0601ACFD,0x0601ADC3},
    {"get_VisualPermil","visual_base_permil","System.Int32",0x0601ACFF,0x0601ADC5},
    {"get_VoteBonusPermil","vote_bonus_permil","System.Int32",0x0601AD01,0x0601ADC7},
    {"get_StarBonusPermil","star_bonus_permil","System.Int32",0x0601AD03,0x0601ADC9},
    {"get_StepSelectNumber","transaction_step_select_number","System.Int32",0x0601AD05,0x0601ADCB}}};

inline std::uint32_t audition_observation_token(std::string_view metadata,
    std::uint32_t original,std::uint32_t updated){
    if(metadata=="9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668")return original;
    if(metadata=="9349bc965fb08a434ab1c9547a3440a8ee02a05e761723792aabe5f4a9ecb635")return updated;
    throw std::runtime_error("audition observation metadata identity unavailable");
}

// Pure assembly seam. Report transaction and current schedule independently:
// normal retry can retain transaction/entry number3 while current schedule is2.
inline json bind_audition_progress_observation(const json& transaction,const json& fields,
    const json& context,const std::string& owner,const json& engine){
    json result={{"schema","gkms.native-audition-progress-observation.v1"},
        {"source","UserDataManager.UserProduceProgressAudition original getters"},
        {"present",!transaction.is_null()},{"transaction",transaction},{"fields",fields},
        {"progress_context",context},{"owner_instance_id",owner},{"engine_identity",engine},
        {"vote_count_conversion_inferred",false},{"input_authority",false}};
    if(transaction.is_null()){
        if(!fields.is_null())throw std::runtime_error("absent audition transaction cannot claim zero fields");
        result["status"]="absent";result["item_fire_counts"]=nullptr;return result;
    }
    if(!transaction.is_object()||!fields.is_object()||fields.size()!=audition_observation_getters.size())
        throw std::runtime_error("audition transaction observation shape differs");
    for(const auto& getter:audition_observation_getters)
        if(!fields.contains(getter.field)||!fields.at(getter.field).is_number_integer())
            throw std::runtime_error("audition observation getter field unavailable");
    const std::array<std::pair<const char*,const char*>,6> names={{{"vocalPermil","vocal_base_permil"},
        {"dancePermil","dance_base_permil"},{"visualPermil","visual_base_permil"},
        {"voteBonusPermil","vote_bonus_permil"},{"starBonusPermil","star_bonus_permil"},
        {"stepSelectNumber","transaction_step_select_number"}}};
    for(const auto& [native,field]:names){
        const json observed=transaction.contains(native)?transaction.at(native):json(0);
        if(!observed.is_number_integer()||observed!=fields.at(field))
            throw std::runtime_error("audition protobuf/getter values changed while copying");
    }
    const auto ids=transaction.value("produceItemIds",json::array());
    const auto counts=transaction.value("produceItemFireCounts",json::array());
    if(!ids.is_array()||!counts.is_array())throw std::runtime_error("audition item counter arrays differ");
    result["status"]="observed";
    result["item_fire_counts"]={{"produce_item_ids",ids},{"produce_item_fire_counts",counts},
        {"parallel_lengths_match",ids.size()==counts.size()},
        {"source","original UserProduceProgressAudition protobuf repeated fields; no missing counts synthesized"}};
    return result;
}

json read_audition_progress_observation(Runtime& runtime);
}
