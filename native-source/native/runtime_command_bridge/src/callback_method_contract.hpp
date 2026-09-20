#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
// The source token belongs to this exact declaring identity, not the object
// receiving the callback and not a global RID namespace.
inline json callback_method_source(const char* image,const char* ns,
    std::initializer_list<const char*> type_path,const char* name,int arity,std::uint32_t token){
    auto path=json::array();for(const auto* part:type_path)path.push_back(part);
    return {{"image",image},{"namespace",ns},{"type_path",path},
        {"name",name},{"arity",arity},{"token",token}};
}

// Pure fixture seam. Resolve is called only after the actual reflected method
// matches its explicit source role; it must use the verified version profile.
template<class Resolve>
std::uint32_t qualified_callback_token(const json& actual,const json& source,Resolve resolve){
    if(!actual.is_object()||!source.is_object())return 0;
    for(const auto* key:{"image","namespace","type_path","name","arity"})
        if(!actual.contains(key)||!source.contains(key)||actual.at(key)!=source.at(key))return 0;
    if(!actual.contains("token")||!source.contains("token")||
       !actual.at("token").is_number_integer()||!source.at("token").is_number_integer())return 0;
    const auto observed=actual.at("token").get<std::int64_t>();
    const auto original=source.at("token").get<std::int64_t>();
    if(observed<0||original<0||observed>0x06FFFFFF||original>0x06FFFFFF||
       (observed>>24)!=6||(original>>24)!=6)return 0;
    const auto expected=resolve(static_cast<std::uint32_t>(original));
    return expected&&observed==expected?expected:0;
}

// Only inspects this existing reflected MethodInfo. It does not invoke the
// callback, change its target, register a delegate or own a game operation.
std::uint32_t read_profiled_callback_token(Runtime& runtime,void* reflected_method,
    const json& source);
}
