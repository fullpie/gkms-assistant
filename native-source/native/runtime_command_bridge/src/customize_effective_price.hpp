#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline constexpr const char* customization_price_schema="gkms.customize-effective-price.v1";

inline json bind_customization_price(int base,int effective,const json& context){
    if(base<0||effective<0||!context.at("pricing_wallet").is_number_integer()||
       context.at("pricing_wallet").get<int>()<0||!context.at("pricing_step_type").is_number_integer()||
       !context.at("pricing_discount_permils").is_array())
        throw std::runtime_error("native effective customization price/context is invalid");
    for(const auto& item:context.at("pricing_discount_permils"))
        if(!item.is_number_integer())throw std::runtime_error("native customization discount is not Int32");
    auto value=context;
    value.update({{"customization_price_schema",customization_price_schema},
        {"base_produce_points",base},{"produce_points",effective},
        {"pricing_source","native Master.ProduceCardCustomize.GetAffectedConsumptionPoint"}});
    return value;
}
inline bool customization_price_affordable(const json& quote){
    return quote.at("produce_points").get<int>()<=quote.at("pricing_wallet").get<int>();
}
inline void bind_customization_price_target(json& target,const json& quote){
    for(const auto* key:{"customization_price_schema","base_produce_points","produce_points",
                         "pricing_step_type","pricing_discount_permils","pricing_wallet"})
        target[key]=quote.at(key);
}
inline void require_customization_price_target(const json& target,const json& quote){
    for(const auto* key:{"customization_price_schema","base_produce_points","produce_points",
                         "pricing_step_type","pricing_discount_permils","pricing_wallet"})
        if(!target.contains(key)||
           (!(target.at(key).is_number_integer()&&quote.at(key).is_number_integer())&&target.at(key).type()!=quote.at(key).type())||
           target.at(key)!=quote.at(key))
            throw std::runtime_error("native customization effective price changed before input");
    if(!customization_price_affordable(quote))
        throw std::runtime_error("native customization effective price exceeds current wallet");
}
}
