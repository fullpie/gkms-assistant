#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
inline bool native_audition_result_parent(const std::string& name){
    return name=="AuditionBattleResultScreenPresenter"||name=="AuditionBattleResultHifScreenPresenter";
}
constexpr bool native_free_retry_available(int free_count,int remaining_count){return free_count>0&&remaining_count>0;}
inline bool native_single_ticket_retry(const std::string& item_id,std::int64_t balance,int free_count,int remaining_count,int price,int max_cost){
    return item_id=="item-produce_continue-1"&&balance>=1&&free_count==0&&remaining_count>0&&price==1&&max_cost==1;
}
bool append_audition_retry_actions(Runtime&,void*,const std::string&,json&);
bool submit_audition_retry_action(Runtime&,void*,const json&,const json&);
}
