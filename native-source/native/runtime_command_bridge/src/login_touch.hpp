#pragma once
#include <nlohmann/json.hpp>
#include <string>

namespace gkms::bridge {
inline bool native_login_screen(const std::string& screen){
    return screen=="DailyLoginBonusScreenPresenter"||screen=="EventSimpleLoginBonusScreenPresenter"||
        screen=="EventSpecialLoginBonusScreenPresenter";
}
inline nlohmann::json native_login_touch_target(const nlohmann::json& state){
    using nlohmann::json;
    if(state.value("schema",std::string())!="gkms.login-touch.v1"||
       !native_login_screen(state.value("owner_type",std::string()))||
       state.value("active",false)!=true||state.value("enabled",false)!=true||state.value("disabled",true)!=false)
        return nullptr;
    auto identity=[](const json& row,const char* name){
        const auto it=row.find(name);
        return it!=row.end()&&it->is_string()&&!it->get_ref<const std::string&>().empty()&&*it!="0x0";
    };
    if(!identity(state,"owner_instance_id")||!identity(state,"button_instance_id"))return nullptr;
    const auto waiters=state.find("waiters");
    if(waiters==state.end()||!waiters->is_array())return nullptr;
    json pending=json::array();
    for(const auto& waiter:*waiters){
        if(!waiter.is_object()||waiter.value("source",std::string())!="CampusButtonBase.WaitClickAsync")continue;
        const auto status=waiter.find("status");
        if(status==waiter.end()||!status->is_number_integer()||status->get<int>()!=0)continue;
        if(!identity(waiter,"closure_instance_id")||!identity(waiter,"completion_source_instance_id"))continue;
        pending.push_back({{"closure_instance_id",waiter.at("closure_instance_id")},
                           {"completion_source_instance_id",waiter.at("completion_source_instance_id")}});
    }
    if(pending.empty())return nullptr;
    return {{"action_id","ui.navigation"},{"button_id","login.continue"},
        {"owner_instance_id",state.at("owner_instance_id")},{"button_instance_id",state.at("button_instance_id")},
        {"pending_waiters",pending}};
}
}
