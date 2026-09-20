#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline bool live_presentation_identity_matches(const json& progress,const json& live,int active_count,bool has_layer){
    if(active_count!=1||has_layer||!progress.is_object()||!live.is_object()||
        !progress.value("in_progress",false)||progress.value("produce_id",std::string()).empty()||
        live.value("live_from_type",0)!=1)return false;
    for(const auto* key:{"idol_card_id","character_id"}){
        const auto actual=progress.value(key,std::string());
        if(actual.empty()||actual!=live.value(key,std::string()))return false;
    }
    return true;
}
inline std::string live_presentation_phase(const json& live){
    if(live.value("is_pause",false))return "paused";
    if(live.value("is_playing",false))return "playing";
    return live.value("is_live_ended",false)?"after_live":"before_live";
}
// Pure envelope shared by the native reader and offline fixture. Observation
// timestamps never change the revision; true timeline/state changes do.
inline json make_live_presentation_snapshot(const std::string& generation,const std::string& presenter_id,
    json progress,json state,json live,const std::string& captured_at){
    if(!live_presentation_identity_matches(state,live,1,false))throw std::runtime_error("live presentation does not bind current produce identity");
    live["family"]="live_presentation";live["phase"]=live_presentation_phase(live);
    live["identity_bound"]=true;
    live["source"]="unique active LiveScenePresenter + LiveModel.FixedData/ExecutionData + current UserProduceProgress";
    live["input_projection"]="read-only; live skip/photo/menu controls are not projected";
    live["loading_fallback_supported"]=false;
    json snapshot={{"schema","gkms.outer-runtime-snapshot.v1"},{"screen_type","LiveScenePresenter"},
        {"underlying_screen_type","LiveScenePresenter"},{"screen_instance_id",presenter_id},
        {"surface","presentation"},{"busy",live.value("is_playing",false)&&!live.value("is_pause",false)},
        {"busy_source","native LiveExecutionData.IsPlaying && !IsPause"},{"window_root_available",false},
        {"state",state},{"progress",progress},{"state_available",true},{"progress_available",true},
        {"collections",json::object()},{"collections_available",false},{"ui_state",live},
        {"legal_actions",json::array()},{"actions_complete",false},{"blockers",json::array()}};
    snapshot["revision"]=sha256(generation+snapshot.dump());snapshot["captured_at"]=captured_at;
    return snapshot;
}
// Null means no positive active, same-produce Live owner was observed. Loading
// without a presenter is intentionally not inferred from status 19 or absence.
json read_live_presentation_snapshot(Runtime&,const std::string& generation);
bool submit_live_presentation_action(Runtime&,const json& target,const json& before);
}
