#include "callback_method_contract.hpp"
#include "pc_method_binding.hpp"
#include <vector>

namespace gkms::bridge {
namespace {
template<class T>T metadata_api(const char* name){
    const auto module=GetModuleHandleW(L"GameAssembly.dll");
    const auto symbol=module?GetProcAddress(module,name):nullptr;
    if(!symbol)throw std::runtime_error(std::string("callback metadata export unavailable: ")+name);
    return reinterpret_cast<T>(symbol);
}
std::string metadata_text(const char* value){
    if(!value)throw std::runtime_error("callback declaring identity string unavailable");
    return value;
}
json callback_declaring_identity(void* klass){
    if(!klass)throw std::runtime_error("callback declaring class unavailable");
    const auto name=metadata_api<const char*(*)(void*)>("il2cpp_class_get_name");
    const auto declaring=metadata_api<void*(*)(void*)>("il2cpp_class_get_declaring_type");
    std::vector<std::string> names;auto outer=klass;
    for(auto current=klass;current;current=declaring(current)){
        if(names.size()>=32)throw std::runtime_error("callback declaring chain exceeds bound");
        names.push_back(metadata_text(name(current)));outer=current;
    }
    auto path=json::array();for(auto it=names.rbegin();it!=names.rend();++it)path.push_back(*it);
    const auto image=metadata_api<void*(*)(void*)>("il2cpp_class_get_image")(klass);
    if(!image)throw std::runtime_error("callback declaring image unavailable");
    return {{"image",metadata_text(metadata_api<const char*(*)(void*)>("il2cpp_image_get_name")(image))},
        {"namespace",metadata_text(metadata_api<const char*(*)(void*)>("il2cpp_class_get_namespace")(outer))},
        {"type_path",path}};
}
}

std::uint32_t read_profiled_callback_token(Runtime& r,void* reflected_method,const json& source){
    if(!r.managed_thread()||!r.operation_active())throw std::runtime_error("callback inspection requires managed owner operation");
    if(!reflected_method)return 0;
    auto declaring_type=r.getter(reflected_method,"get_DeclaringType");
    if(!declaring_type)return 0;
    auto klass=metadata_api<void*(*)(void*)>("il2cpp_class_from_system_type")(declaring_type);
    auto observed=callback_declaring_identity(klass);
    const auto name=r.string(r.getter(reflected_method,"get_Name"));
    const auto parameters=r.enumerate(r.getter(reflected_method,"GetParameters"),128);
    const auto token=r.unbox<std::int32_t>(r.getter(reflected_method,"get_MetadataToken"));
    observed.update({{"name",name},{"arity",parameters.size()},{"token",token}});
    return qualified_callback_token(observed,source,[&](std::uint32_t original){
        return pc_binding_token(klass,name.c_str(),static_cast<int>(parameters.size()),original);
    });
}
}
