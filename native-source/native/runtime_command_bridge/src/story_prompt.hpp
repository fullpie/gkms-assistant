#pragma once
#include <string>
namespace gkms::bridge {
inline bool is_unread_story_skip_prompt(std::string title,std::string description){
    auto value=title+"\n"+description;
    for(auto& ch:value)if(ch>='A'&&ch<='Z')ch=static_cast<char>(ch+('a'-'A'));
    const auto contains=[&](const char8_t* text){return value.find(reinterpret_cast<const char*>(text))!=std::string::npos;};
    const bool unread=contains(u8"\u672a\u8aad")||contains(u8"\u672a\u8b80")||contains(u8"\u672a\u8bfb")||value.find("unread")!=std::string::npos;
    const bool skip=contains(u8"\u30b9\u30ad\u30c3\u30d7")||contains(u8"\u8df3\u904e")||contains(u8"\u8df3\u8fc7")||value.find("skip")!=std::string::npos;
    return unread&&skip;
}
}
