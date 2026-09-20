#pragma once
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <set>

namespace gkms::bridge {
// Candidate identity is the game's composite Master key. ID alone repeats
// across audition stages and difficulty numbers. No ordering is inferred.
inline nlohmann::json audition_selection_actions(const nlohmann::json& candidates,
                                                int selected_index) {
    using json=nlohmann::json;
    json actions=json::array();
    std::set<std::string> identities;
    for(std::size_t index=0;index<candidates.size();++index){
        const auto& candidate=candidates.at(index);
        const auto& identity=candidate.at("identity");
        if(identity.at("difficulty_id").get<std::string>().empty()||
            identity.at("produce_id").get<std::string>().empty()||
            !identities.insert(identity.dump()).second)
            throw std::runtime_error("audition candidate identity missing or repeated");
        if(candidate.at("enabled")!=true)continue;
        json target=identity;
        target["index"]=index;
        target["action_id"]=static_cast<int>(index)==selected_index?"audition.enter":"audition.choose";
        actions.push_back({{"action_id",target.at("action_id")},{"target",target},
            {"evidence",{{"difficulty",candidate.at("difficulty")},
                {"source","current CandidateAuditionList and normal select/decide callbacks"}}}});
    }
    return actions;
}
}
