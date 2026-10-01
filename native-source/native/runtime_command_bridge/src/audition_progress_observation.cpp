#include "audition_progress_observation.hpp"
#include "pc_method_binding.hpp"
#include "pointer_identity.hpp"
#include "produce_lifecycle_adapter.hpp"

namespace gkms::bridge {
namespace {
template<class T>T metadata_api(const char* name){
    auto module=GetModuleHandleW(L"GameAssembly.dll");
    const auto symbol=module?GetProcAddress(module,name):nullptr;
    if(!symbol)throw std::runtime_error(std::string("audition metadata export unavailable: ")+name);
    return reinterpret_cast<T>(symbol);
}
void* checked_getter(Runtime& r,void* klass,const char* name,const char* owner_ns,
    const char* owner_name,const char* return_type,bool is_static,std::uint32_t expected){
    auto method=r.method(klass,name,0);
    auto declaring=metadata_api<void*(*)(void*)>("il2cpp_method_get_class")(method);
    const auto token=metadata_api<std::uint32_t(*)(void*)>("il2cpp_method_get_token")(method);
    if(!declaring||r.class_name(declaring)!=owner_name||r.class_namespace(declaring)!=owner_ns||token!=expected||
        r.method_result_contract(method)!=json{{"is_static",is_static},{"return_type",return_type}})
        throw std::runtime_error(std::string("audition observation getter contract differs: ")+name);
    return method;
}
}

json read_audition_progress_observation(Runtime& r){
    if(!r.managed_thread()||!r.operation_active())throw std::runtime_error("audition observation requires managed owner");
    const auto engine=verified_pc_binding_identity();
    const auto metadata=engine.at("metadata_sha256").get<std::string>();
    const auto manager=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    const auto get=checked_getter(r,manager,"get_UserProduceProgressAudition","Campus.Common.User",
        "UserDataManagerBase`1","Campus.Common.Proto.Client.Transaction.UserProduceProgressAudition",true,
        audition_observation_token(metadata,0x06016CB0,0x06016D26));
    auto transaction=r.invoke(get,nullptr);
    const auto context=read_produce_context(r);
    if(!transaction)return bind_audition_progress_observation(nullptr,nullptr,context,"0x0",engine);
    const auto type=r.object_class(transaction);
    constexpr const char* ns="Campus.Common.Proto.Client.Transaction";
    constexpr const char* name="UserProduceProgressAudition";
    if(r.class_name(type)!=name||r.class_namespace(type)!=ns)
        throw std::runtime_error("current audition transaction type differs");
    auto stringify=checked_getter(r,type,"ToString",ns,name,"System.String",false,
        audition_observation_token(metadata,0x0601AD18,0x0601ADDE));
    const auto original=json::parse(r.string(r.invoke(stringify,transaction)));
    auto fields=json::object();
    for(const auto& field:audition_observation_getters){
        auto method=checked_getter(r,type,field.name,ns,name,field.return_type,false,
            audition_observation_token(metadata,field.original_token,field.updated_token));
        fields[field.field]=r.unbox<int>(r.invoke(method,transaction));
    }
    if(r.invoke(get,nullptr)!=transaction||json::parse(r.string(r.invoke(stringify,transaction)))!=original)
        throw std::runtime_error("audition transaction changed during observation");
    return bind_audition_progress_observation(original,fields,context,pointer_identity(transaction),engine);
}
}
