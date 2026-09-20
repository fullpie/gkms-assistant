#pragma once
#include "runtime.hpp"
#include "pointer_identity.hpp"
#include <algorithm>

namespace gkms::bridge {
// Read a bounded prefix from an already resolved executable method in this
// process. This never invokes the method, changes page protection, installs a
// hook, attaches a thread, or reads unrelated process memory.
inline json selector_method_code(Runtime& r,void* method){
    auto pointer=r.method_pointer(method);
    MEMORY_BASIC_INFORMATION page{};
    const auto address=reinterpret_cast<std::uintptr_t>(pointer);
    json result={{"method_id",pointer_identity(method)},{"code_id",pointer_identity(pointer)},
        {"parameters",r.method_parameter_contract(method)}};
    if(!pointer||!VirtualQuery(pointer,&page,sizeof(page))||page.State!=MEM_COMMIT||
        (page.Protect&(PAGE_GUARD|PAGE_NOACCESS))||
        !(page.Protect&(PAGE_EXECUTE_READ|PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY))){
        result["code_available"]=false;return result;
    }
    const auto end=reinterpret_cast<std::uintptr_t>(page.BaseAddress)+page.RegionSize;
    if(end<=address){result["code_available"]=false;return result;}
    const auto count=std::min<std::size_t>(4096,end-address);
    const auto bytes=static_cast<const unsigned char*>(pointer);
    constexpr char hex[]="0123456789abcdef";
    std::string encoded;encoded.reserve(count*2);
    for(std::size_t i=0;i<count;++i){encoded.push_back(hex[bytes[i]>>4]);encoded.push_back(hex[bytes[i]&15]);}
    result.update({{"code_available",true},{"prefix_bytes",count},{"prefix_hex",encoded},
        {"game_assembly_base",pointer_identity(GetModuleHandleW(L"GameAssembly.dll"))}});
    return result;
}
inline json selector_owner_diagnostic(Runtime& r,void* selector){
    auto klass=r.object_class(selector);
    auto game_object=r.getter(selector,"get_gameObject");
    auto trigger_type=r.reflection_type(r.klass("UniTask.dll","Cysharp.Threading.Tasks.Triggers","AsyncDestroyTrigger"));
    auto component_method=r.method(r.object_class(game_object),"GetComponent",1,0x06001412);
    auto trigger=r.invoke(component_method,game_object,{trigger_type});
    json result={{"schema","gkms.exam-selector-owner-diagnostic.v1"},
        {"selector_id",pointer_identity(selector)},{"game_object_id",pointer_identity(game_object)},
        {"trigger_id",pointer_identity(trigger)},
        {"trigger_type",trigger?json(r.class_name(r.object_class(trigger))):json(nullptr)},
        {"trigger_namespace",trigger?json(r.class_namespace(r.object_class(trigger))):json(nullptr)},
        {"component_method_parameters",r.method_parameter_contract(component_method)},
        {"components",json::array()},{"methods",json::object()},{"inspected_async_methods_invoked",false}};
    auto component_type=r.reflection_type(r.klass("UnityEngine.CoreModule.dll","UnityEngine","Component"));
    auto array=r.invoke(r.method(r.object_class(game_object),"GetComponents",1,0x06001420),game_object,{component_type});
    for(auto component:r.enumerate(array,128))if(component)result["components"].push_back({
        {"id",pointer_identity(component)},{"type",r.class_name(r.object_class(component))},
        {"namespace",r.class_namespace(r.object_class(component))}});
    // Cache only method bytes; current component inventory is always reread.
    // Compiler state-machine indexes are pinned by the current PC metadata.
    static json code;
    if(code.is_null()){
        code=json::object();
        try{
            code["WaitSelectAsync"]=selector_method_code(r,r.method(klass,"WaitSelectAsync",1,0x06005E71));
            code["WaitSelectAsync.MoveNext"]=selector_method_code(r,r.method(r.nested_class(klass,"<WaitSelectAsync>d__52"),"MoveNext",0));
            code["GameObject.GetComponent.Type"]=selector_method_code(r,component_method);
        }catch(const std::exception& error){code["error"]=error.what();}
    }
    result["methods"]=code;
    return result;
}
} // namespace gkms::bridge
