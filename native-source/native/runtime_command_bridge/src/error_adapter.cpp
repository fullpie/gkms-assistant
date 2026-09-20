#include "error_adapter.hpp"
#include "pointer_identity.hpp"
#include <string>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* o,const char* getter){return r.unbox<bool>(r.getter(o,getter));}
bool active(Runtime& r,void* o){return o&&flag(r,r.getter(o,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* b){return active(r,b)&&flag(r,b,"get_IsEnabled")&&!flag(r,b,"get_IsDisabled")&&r.read_object_field(b,"onClickedCallback");}
void* current_error(Runtime& r){
    auto object=r.klass("UnityEngine.CoreModule","UnityEngine","Object");
    auto type=r.reflection_type(r.klass("Assembly-CSharp","Campus.Common","ErrorSheetPresenter"));
    auto values=r.invoke(r.method(object,"FindObjectsOfType",1,0x06001546),nullptr,{type});
    void* result{};
    for(auto value:r.enumerate(values,8))if(active(r,value)){
        if(result)throw std::runtime_error("multiple active native error sheets");result=value;
    }
    return result;
}
bool return_title_label(std::string label){
    for(auto& c:label)if(c>='A'&&c<='Z')c=static_cast<char>(c+('a'-'A'));
    const auto has=[&](const char8_t* text){return label.find(reinterpret_cast<const char*>(text))!=std::string::npos;};
    return (has(u8"\u30bf\u30a4\u30c8\u30eb")&&(has(u8"\u623b")||has(u8"\u3078")))||
        ((has(u8"\u6a19\u984c")||has(u8"\u6807\u9898"))&&(has(u8"\u8fd4\u56de")||has(u8"\u56de\u5230")))||
        (label.find("title")!=std::string::npos&&(label.find("return")!=std::string::npos||label.find("back")!=std::string::npos));
}
json prompt(Runtime& r,void* error){
    auto view=r.read_object_field(error,"_view");auto common=r.getter(view,"get_CommonView");
    const auto title=r.string(r.getter(r.getter(common,"get_Title"),"get_text"));
    const auto description=r.string(r.getter(r.read_object_field(view,"_description"),"get_text"));
    return {{"title",title},{"description",description},{"digest",sha256(json::array({title,description}).dump())}};
}
void* error_button(Runtime& r,void* error,const std::string& source){
    auto common=r.getter(r.read_object_field(error,"_view"),"get_CommonView");
    if(source!="cancel"&&source!="execute")throw std::runtime_error("unknown error control");
    return r.getter(common,source=="cancel"?"get_CancelButton":"get_ExecuteButton");
}
std::string label(Runtime& r,void* button){return r.string(r.getter(r.getter(button,"get_Text"),"get_text"));}
}
json read_error_snapshot(Runtime& r,const std::string& generation){
    auto error=current_error(r);if(!error)return nullptr;
    auto detail=prompt(r,error);const bool closing=flag(r,error,"get_IsClosing");
    json snapshot={{"schema","gkms.outer-runtime-snapshot.v1"},{"surface","error"},{"screen_type","ErrorSheetPresenter"},
        {"screen_instance_id",pointer_identity(error)},{"underlying_screen_type",nullptr},{"busy",closing},
        {"state",nullptr},{"progress",nullptr},{"state_available",false},{"progress_available",false},
        {"collections",json::object()},{"selection",nullptr},{"ui_state",{{"error",detail},{"closing",closing}}},
        {"blockers",json::array()},{"actions_complete",true},{"legal_actions",json::array()}};
    if(!closing)for(const auto* source:{"cancel","execute"}){
        auto button=error_button(r,error,source);
        if(!clickable(r,button)||!return_title_label(label(r,button)))continue;
        json target={{"action_id","error.return_title"},{"sheet_instance_id",pointer_identity(error)},
            {"prompt_digest",detail.at("digest")},{"button_source",source},{"button_instance_id",pointer_identity(button)},
            {"callback_instance_id",pointer_identity(r.read_object_field(button,"onClickedCallback"))}};
        snapshot["legal_actions"].push_back({{"action_id","error.return_title"},{"target",target}});
    }
    snapshot["revision"]=sha256(generation+snapshot.dump());snapshot["captured_at"]=utc_now();return snapshot;
}
void submit_error_return(Runtime& r,const json& target,const json&){
    auto error=current_error(r);
    if(!error||pointer_identity(error)!=target.at("sheet_instance_id").get<std::string>()||prompt(r,error).at("digest")!=target.at("prompt_digest"))
        throw std::runtime_error("foreground error sheet changed");
    auto button=error_button(r,error,target.at("button_source").get<std::string>());
    if(!clickable(r,button)||!return_title_label(label(r,button))||pointer_identity(button)!=target.at("button_instance_id").get<std::string>()||
        pointer_identity(r.read_object_field(button,"onClickedCallback"))!=target.at("callback_instance_id").get<std::string>())
        throw std::runtime_error("error return-title control changed");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
}
}
