#pragma once
#include "runtime.hpp"
#include <map>
#include <set>

namespace gkms::bridge {
// Managed-thread only. These observe/consume only the task owned by an explicit
// loadout.memory_auto request, never an arbitrary task found in the game.
json read_memory_auto(Runtime&);
bool append_memory_auto_actions(Runtime&,void*,const std::string&,json&);
bool submit_memory_auto_action(Runtime&,void*,const json&,const json&);
void validate_memory_auto_override(Runtime&,const json& target,const json& loadout);
void mark_memory_auto_override_started(Runtime&);

inline bool memory_auto_resources_complete(const json& resources) {
    if(!resources.is_array()||resources.size()!=4)return false;
    std::set<int> positions;std::set<std::string> ids;
    for(const auto& row:resources){
        if(!row.is_object()||!row.contains("position")||!row["position"].is_number_integer()||
           !row.contains("memory_id")||!row["memory_id"].is_string()||
           !row.contains("is_rental")||!row["is_rental"].is_boolean())return false;
        const auto position=row["position"].get<int>();const auto id=row["memory_id"].get<std::string>();
        if(position<0||position>3||id.empty()||!positions.insert(position).second||!ids.insert(id).second)return false;
    }
    return true;
}

// Preserve the game's other slots, including rental identity. Conflicting
// locks fail rather than moving/rebuilding an unlocked game-selected slot.
inline bool memory_auto_same_resources(const json& a,const json& b) {
    if(!memory_auto_resources_complete(a)||!memory_auto_resources_complete(b))return false;
    std::map<int,std::pair<std::string,bool>> left,right;
    for(const auto& row:a)left.emplace(row.at("position").get<int>(),std::make_pair(row.at("memory_id").get<std::string>(),row.at("is_rental").get<bool>()));
    for(const auto& row:b)right.emplace(row.at("position").get<int>(),std::make_pair(row.at("memory_id").get<std::string>(),row.at("is_rental").get<bool>()));
    return left==right;
}

inline json memory_auto_override_resources(const json& resources,const json& overrides) {
    if(!memory_auto_resources_complete(resources)||!overrides.is_array()||overrides.empty()||overrides.size()>4)
        throw std::runtime_error("memory-auto-invalid-lock-projection");
    json expected=resources;std::set<int> seen;
    for(const auto& lock:overrides){
        if(!lock.is_object()||lock.size()!=3||!lock.contains("position")||!lock["position"].is_number_integer()||
           !lock.contains("before_memory_id")||!lock["before_memory_id"].is_string()||
           !lock.contains("locked_memory_id")||!lock["locked_memory_id"].is_string())
            throw std::runtime_error("memory-auto-invalid-lock-fields");
        const int position=lock["position"].get<int>();const auto id=lock["locked_memory_id"].get<std::string>();
        if(position<0||position>3||id.empty()||!seen.insert(position).second)
            throw std::runtime_error("memory-auto-invalid-lock-position");
        bool found=false;
        for(auto& row:expected)if(row["position"]==position){
            if(row["memory_id"]!=lock["before_memory_id"])throw std::runtime_error("memory-auto-lock-before-id-changed");
            row["memory_id"]=id;row["is_rental"]=false;found=true;
        }
        if(!found)throw std::runtime_error("memory-auto-lock-slot-missing");
    }
    if(!memory_auto_resources_complete(expected))throw std::runtime_error("memory-auto-lock-conflicts-with-preserved-slot");
    return expected;
}
}
