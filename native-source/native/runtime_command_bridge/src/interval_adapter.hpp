#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline bool interval_product_eligible(const json& product,int points){
    if(product.value("can_buy",false)!=true||product.value("purchased",true)!=false||
        !product.contains("price")||!product.at("price").is_number_integer())return false;
    const auto price=product.at("price").get<int>();
    if(price<0||price>points)return false;
    if(product.contains("remaining_count")&&!product.at("remaining_count").is_null()&&product.at("remaining_count").get<int>()<=0)return false;
    return true;
}
inline bool interval_target_matches_product(const json& target,const json& product){
    for(const auto* key:{"group","index","resource_type","position_number","resource_id","price","product_instance_id"})
        if(!target.contains(key)||!product.contains(key)||target.at(key)!=product.at(key))return false;
    return true;
}
// This binds the full *current pool*, not a pending modal quote. The normal
// ShopConfirm model has no retained product/price, so the host must additionally
// retain the original submitted operation and any actual card-selector result.
inline std::string interval_confirmation_fingerprint(json state){
    state.erase("products_fingerprint");state.erase("confirmation_binding");
    return sha256(state.dump());
}
inline bool interval_confirmation_matches(const json& target,const json& state,const std::string& parent){
    return !parent.empty()&&target.value("parent_instance_id",std::string())==parent&&
        target.value("products_fingerprint",std::string())==interval_confirmation_fingerprint(state);
}
bool append_interval_actions(Runtime&,void* presenter,const std::string& screen,json& snapshot);
bool submit_interval_action(Runtime&,void* presenter,const json& target,const json& before);
}
