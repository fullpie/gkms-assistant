#pragma once
#include <nlohmann/json.hpp>
#include <set>
#include <stdexcept>
#include <string>
namespace gkms::bridge {
inline std::string diagnostic_utf8_prefix(const std::string& value,std::size_t limit){
    auto end=std::min(value.size(),limit);
    // Runtime.string produces valid UTF-8. Exclude the whole final codepoint
    // if the byte bound lands inside it, so Mailbox.dump remains serializable.
    if(end<value.size())while(end>0&&(static_cast<unsigned char>(value[end])&0xc0)==0x80)--end;
    return value.substr(0,end);
}
inline void validate_research_query(const nlohmann::json& query){
    using nlohmann::json;
    auto require=[](bool ok,const char* why){if(!ok)throw std::runtime_error(why);};
    auto bounded=[](const json& v,std::uint64_t low,std::uint64_t high){
        return v.is_number_integer()&&v.get<std::uint64_t>()>=low&&v.get<std::uint64_t>()<=high;
    };
    auto text=[&](const json& v,bool empty=false){
        require(v.is_string(),"diagnostic name must be text");const auto s=v.get<std::string>();
        require((empty||!s.empty())&&s.size()<=256&&s.find('\0')==std::string::npos,"diagnostic name is invalid");
    };
    require(query.is_object(),"diagnostic query must be an object");
    const auto kind=query.at("kind").get<std::string>();
    std::set<std::string> allowed;
    if(kind=="pc_core_image"){
        allowed={"kind"};
    }else if(kind=="method_code"){
        allowed={"kind","assembly","namespace","class","nested_class","method","arity","token"};
        for(const auto* key:{"assembly","class","method"})text(query.at(key));
        text(query.at("namespace"),true);
        if(query.contains("nested_class"))text(query.at("nested_class"));
        const std::set<std::string> assemblies={"Assembly-CSharp.dll","UniTask.dll","UnityEngine.CoreModule.dll","mscorlib.dll"};
        require(assemblies.contains(query.at("assembly").get<std::string>()),"diagnostic assembly outside game metadata scope");
        require(bounded(query.at("arity"),0,16),"diagnostic method arity invalid");
        require(bounded(query.at("token"),1,0xffffffffULL),"diagnostic method token required");
    }else if(kind=="object_fields"){
        allowed={"kind","anchor","path","fields","items"};
        const std::set<std::string> anchors={"selector","exam_screen","exam_sequence"};
        require(anchors.contains(query.at("anchor").get<std::string>()),"diagnostic object requires a live Exam anchor");
        require(query.at("path").is_array()&&query.at("path").size()<=24,"diagnostic field path exceeds limit");
        for(const auto& step:query.at("path")){
            require(step.is_object()&&step.size()==1,"diagnostic path step must select one field or index");
            if(step.contains("field"))text(step.at("field"));
            else require(step.contains("index")&&bounded(step.at("index"),0,4095),"diagnostic array index invalid");
        }
        require(query.at("fields").is_array()&&query.at("fields").size()<=32,"diagnostic field count exceeds limit");
        for(const auto& field:query.at("fields"))text(field);
        if(query.contains("items"))require(bounded(query.at("items"),0,128),"diagnostic item summary limit invalid");
    }else throw std::runtime_error("unsupported read-only diagnostic kind");
    for(const auto& item:query.items())require(allowed.contains(item.key()),"unknown diagnostic field; invocation and raw addresses are not accepted");
}
} // namespace gkms::bridge
