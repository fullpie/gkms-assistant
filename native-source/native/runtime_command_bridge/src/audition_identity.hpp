#pragma once
#include <nlohmann/json.hpp>
namespace gkms::bridge {
inline nlohmann::json current_audition_number(int week,int step,const nlohmann::json& schedule){
    if((step!=16&&step!=17&&step!=18)||!schedule.is_object()||
        schedule.at("step_number")!=week||schedule.at("selected_step_type")!=step)return nullptr;
    return schedule.at("step_select_number");
}
inline bool exam_context_matches(const nlohmann::json& context,const nlohmann::json& save){
    return context.is_object()&&save.contains("stepType")&&save.contains("produceId")&&save.contains("idolCardId")&&
        save.at("stepType")==context.at("step_type")&&save.at("produceId")==context.at("produce_id")&&
        save.at("idolCardId")==context.at("idol_card_id");
}
}
