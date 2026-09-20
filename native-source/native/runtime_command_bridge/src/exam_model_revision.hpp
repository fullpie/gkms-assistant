#pragma once
#include <nlohmann/json.hpp>

namespace gkms::bridge {
inline nlohmann::json reference_presence_revision_view(nlohmann::json proof){
    if(!proof.is_object())return proof;
    proof.erase("save_object_id");proof.erase("status_collection_id");proof.erase("save_status_collection_id");
    if(proof.contains("active_status_sources")&&proof["active_status_sources"].is_array())
        for(auto& row:proof["active_status_sources"]){
            row.erase("owner_object_id");
            row.erase("save_owner_object_id");
            if(row.contains("fields")&&row["fields"].is_array())for(auto& field:row["fields"])field.erase("object_id");
            if(row.contains("save_fields")&&row["save_fields"].is_array())for(auto& field:row["save_fields"])field.erase("object_id");
        }
    return proof;
}
// Keep the entire DTO as evidence. SaveData traversal pointers are not stable
// state identity (the capture may clone them); null bits, types, UIDs, hashes,
// counters and the actual sequence/parameter/command/UI owners remain in CAS.
inline nlohmann::json exam_model_revision_view(nlohmann::json snapshot){
    if(!snapshot.contains("exam_model_observation")||!snapshot["exam_model_observation"].is_object())return snapshot;
    auto& dto=snapshot["exam_model_observation"];
    dto.erase("save_object_id");dto.erase("save_object_id_after");
    for(const auto* key:{"reference_presence","reference_presence_after"})
        if(dto.contains(key))dto[key]=reference_presence_revision_view(dto[key]);
    return snapshot;
}
}
