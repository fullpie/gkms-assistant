#pragma once
#include "runtime.hpp"
#include <set>

namespace gkms::bridge {
// Exactly the existing ExamAdapter submission boundary, independent of any
// simulator, model, training coverage, projection or predicted final score.
inline bool exam_input_boundary_ready(const json& value){
    return value.at("phase")==6&&value.at("busy")==false&&value.at("queue_empty")==true&&
        value.at("terminal")==false&&value.at("turn_card_play_end")==false&&value.at("is_replay")==false;
}
inline json exam_secondary_selection_unknown(){
    return {{"required",nullptr},{"choices_complete",false},{"resolve_at_native_selector",true}};
}
// rows contain only actual native observations. A missing validation result is
// unknown, not false; known legal targets remain inspectable in a partial pool.
inline json make_exam_legal_inputs(const json& boundary,json hand,json drinks){
    const bool ready=exam_input_boundary_ready(boundary);
    json result={{"schema","gkms.native-exam-legal-inputs.v1"},{"scope","primary-inputs-only"},
        {"boundary_ready",ready},{"complete",ready},{"hand",std::move(hand)},{"drinks",std::move(drinks)},
        {"legal_actions",json::array()},{"unknown_checks",json::array()},
        {"simulator_coverage_required",false},{"secondary_selection_complete",false}};
    std::set<std::string> guids;
    std::set<std::string> repeated_guids;
    for(const auto& row:result.at("hand")){
        const auto guid=row.value("card_guid",std::string());
        if(!guid.empty()&&!guids.insert(guid).second)repeated_guids.insert(guid);
    }
    for(const bool card:{true,false}){
        auto& rows=result[card?"hand":"drinks"];
        for(auto& row:rows){
            const char* identity_key=card?"card_guid":"drink_id";
            const auto identity=row.value(identity_key,std::string());
            if(identity.empty()||(card&&repeated_guids.contains(identity))){
                row["can_use"]=nullptr;row["validation_error"]="native-input-identity-unavailable-or-ambiguous";
            }
            if(!row.contains("can_use")||!row.at("can_use").is_boolean()){
                result["complete"]=false;
                result["unknown_checks"].push_back({{"kind",card?"play":"drink"},{"slot",row.at("slot")},
                    {"reason",row.value("validation_error",std::string("native-validation-not-observed"))}});
                continue;
            }
            if(!ready||row.at("can_use")!=true)continue;
            result["legal_actions"].push_back({{"kind",card?"play":"drink"},
                {"command",card?"exam.play":"exam.drink"},
                {"target",{{"slot",row.at("slot")},{identity_key,identity}}},
                {"validation_source",row.at("validation_source")},
                {"secondary_selection",exam_secondary_selection_unknown()}});
        }
    }
    result["end_turn"]={{"can_use",ready},{"validation_source","existing-native-settled-Main-command-boundary"},
        {"separate_validator_available",false}};
    if(ready)result["legal_actions"].push_back({{"kind","end_turn"},{"command","exam.end_turn"},
        {"target",json::object()},{"validation_source",result.at("end_turn").at("validation_source")},
        {"secondary_selection",{{"required",false},{"choices_complete",true},{"resolve_at_native_selector",false}}}});
    return result;
}
}
