#pragma once
#include <stdexcept>
#include <string>

namespace gkms::bridge {
inline bool ordinary_event_choices(const std::string& type){
    if(type=="ChoicesView")return true;
    if(type=="ProduceChoicesView")return false;
    throw std::runtime_error("unsupported native event choices class: "+type);
}
inline bool event_choices_use_produce_suggestions(const std::string& type,bool shown){
    const bool ordinary=ordinary_event_choices(type);
    if(!shown)return false;
    if(ordinary)throw std::runtime_error("visible native ADV choices require their own choice contract");
    return true;
}
}
