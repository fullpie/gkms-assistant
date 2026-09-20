#include "continuation.hpp"
#include <map>
namespace gkms::bridge {
namespace {
bool identity(const json& value){return value.is_string()&&!value.get<std::string>().empty()&&value!="0x0";}
void same_source(const json& command,const json& action,const std::string& kind){
    if(kind=="exam.play"&&command.at("source_card_guid")!=action.at("target").at("card_guid"))
        throw std::runtime_error("selector source card differs from its parent");
    if(kind=="exam.drink"&&command.at("source_drink_id")!=action.at("target").at("drink_id"))
        throw std::runtime_error("selector source drink differs from its parent");
}
}
json latest_manual_main_command(const json& observed_logs){
    if(!observed_logs.is_array())throw std::runtime_error("native manual log list unavailable");
    json latest=nullptr;std::map<std::string,std::size_t> occurrences;std::size_t count=0;
    for(const auto& log:observed_logs){
        if(!log.at("is_select_log").is_boolean()||!log.at("is_manual").is_boolean())
            throw std::runtime_error("native manual log classification unavailable");
        if(log.at("is_select_log")==true||log.at("is_manual")==false)continue;
        if(!identity(log.at("command_native_id")))throw std::runtime_error("native manual log command identity unavailable");
        latest=log;++count;++occurrences[log.at("command_native_id").get<std::string>()];
    }
    if(!latest.is_null())latest["identity_occurrences"]=occurrences[latest.at("command_native_id").get<std::string>()];
    return {{"schema","gkms.exam-manual-root.v1"},{"source","ExamParameterModel.PlayLogList"},
        {"manual_main_count",count},{"latest",latest}};
}
void validate_exam_continuation(const json& request,const json& snapshot,const json& parent,const std::string& generation){
    if(request.value("command",std::string())!="outer.action"||snapshot.value("exam_continuation",false)!=true)
        throw std::runtime_error("continuation requires an actual Exam selector");
    const auto parent_id=request.at("continuation_of").get<std::string>();
    if(request.value("session_generation",std::string())!=generation||
       parent.value("schema",std::string())!="gkms.runtime-command-result.v1"||parent.value("request_id",std::string())!=parent_id||
       parent.value("session_generation",std::string())!=generation||parent.value("status",std::string())!="submitted")
        throw std::runtime_error("continuation parent is not a submitted command in this game session");
    if(!parent.contains("settled")||!parent.at("settled").is_boolean()||parent.at("settled")!=false)
        throw std::runtime_error("continuation parent is already settled or has unknown settlement state");
    const auto& action=parent.at("action");const auto& context=snapshot.at("parent_context");
    if(context.contains("is_replay")&&context.at("is_replay")!=false)
        throw std::runtime_error("replay selector is observation-only");
    const auto kind=action.at("command").get<std::string>();
    if(kind!="exam.play"&&kind!="exam.drink"&&kind!="exam.end_turn")throw std::runtime_error("continuation parent is not an Exam main command");
    for(const auto* field:{"sequence_id","created_command_id"})if(!action.contains(field)||!identity(action[field]))
        throw std::runtime_error("continuation parent has no native command identity");
    if(context.at("sequence_id")!=action.at("sequence_id")||!identity(context.at("current_command_native_id")))
        throw std::runtime_error("selector native sequence/command differs from its parent");
    same_source(context,action,kind);
    if(context.at("current_command_native_id")==action.at("created_command_id")){
        if(context.value("is_manual",false)!=true||context.at("play_type")!=action.at("native_play_type")||context.at("play_index")!=action.at("native_play_index"))
            throw std::runtime_error("selector command metadata differs from its parent");
        return;
    }
    // A normal UseHand/UseDrink command expands into non-manual PlayEffect
    // commands. ExamPlayLog.Command retains the *original* command object,
    // rather than its DeepCopy, so the latest manual log is an exact root ID.
    if((kind!="exam.play"&&kind!="exam.drink")||context.value("is_command_playing",false)!=true||
       !context.contains("is_manual")||context.at("is_manual")!=false||context.value("is_replay",true)!=false||context.at("play_type")!=5||
       !context.contains("effect_id")||!identity(context.at("effect_id")))
        throw std::runtime_error("selector is not a proven downstream PlayEffect command");
    const auto& history=context.at("manual_root");
    if(history.value("schema",std::string())!="gkms.exam-manual-root.v1"||
       history.value("source",std::string())!="ExamParameterModel.PlayLogList")
        throw std::runtime_error("selector native manual root source unavailable");
    const auto& root=history.at("latest");
    if(!root.is_object()||root.at("command_native_id")!=action.at("created_command_id")||
       root.value("is_manual",false)!=true||root.value("is_select_log",true)!=false||
       root.value("is_cost_failed",true)!=false||root.at("identity_occurrences")!=1||
       root.at("play_type")!=action.at("native_play_type")||root.at("play_index")!=action.at("native_play_index")||
       !context.at("native_turn").is_number_integer()||context.at("native_turn").get<int>()<1||root.at("turn")!=context.at("native_turn"))
        throw std::runtime_error("selector does not belong to the unique current manual parent");
    same_source(root,action,kind);
}
}
