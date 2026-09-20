#include "runtime_command_bridge_loader.hpp"
#include <Windows.h>
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <array>
#include <vector>
#include <algorithm>
#include "deps/nlohmann/json.hpp"
#include "public_runtime_paths.hpp"
#include "gkmsTelemetryBootstrap.hpp"

namespace gkms::runtime_command_bridge_loader {
namespace {
void* player_loop_class{};
std::vector<void*> approved_player_loop_methods;
constexpr std::array<const char*,17> player_loop_wrappers={"Run","Initialization","LastInitialization","EarlyUpdate","LastEarlyUpdate",
    "FixedUpdate","LastFixedUpdate","PreUpdate","LastPreUpdate","Update","LastUpdate","PreLateUpdate","LastPreLateUpdate",
    "PostLateUpdate","LastPostLateUpdate","TimeUpdate","LastTimeUpdate"};
void* (*object_class)(void*){};
std::mutex diagnostic_mutex;
nlohmann::json diagnostic=nlohmann::json::object();
void write_diagnostic() {
    diagnostic["schema"]="gkms.runtime-command-loader-diagnostic.v1";
    diagnostic["pid"]=GetCurrentProcessId();diagnostic["updated_tick_ms"]=GetTickCount64();
    const auto root=
        gkms::public_runtime_paths::state_root()/L"runtime_command_bridge";
    std::filesystem::create_directories(root);
    const auto destination=root/(L"loader_diagnostic_"+std::to_wstring(GetCurrentProcessId())+L".json");
    const auto temporary=std::filesystem::path(destination.wstring()+L".tmp");
    {std::ofstream output(temporary,std::ios::binary|std::ios::trunc);output<<diagnostic.dump(2);output.flush();if(!output)return;}
    MoveFileExW(temporary.c_str(),destination.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH);
}
void mark(const char* key,nlohmann::json value,bool once=false) noexcept {
    try{std::lock_guard lock(diagnostic_mutex);if((once&&diagnostic.contains(key))||(diagnostic.contains(key)&&diagnostic[key]==value))return;diagnostic[key]=std::move(value);write_diagnostic();}catch(...){ }
}
void* reject(const char* reason) noexcept {mark("resolve_result",reason);return nullptr;}
template<class T>T api(HMODULE module,const char* name){return reinterpret_cast<T>(GetProcAddress(module,name));}
}
void* unique_player_loop_run() noexcept {
#if defined(GKMS_RUNTIME_COMMAND_BRIDGE)
    try {
    // The current PC compiler folds Run and all sixteen official UniTask
    // player-loop phase wrappers into this one body. Accept the complete
    // metadata-pinned group, and inspect the full inventory for other aliases.
    mark("resolution_thread_id",GetCurrentThreadId());
    mark("target",{{"assembly","UniTask.dll"},{"namespace","Cysharp.Threading.Tasks.Internal"},{"class","PlayerLoopRunner"},{"method","Run"},{"token",0x06000CD5},{"arity",0}});
    auto module=GetModuleHandleW(L"GameAssembly.dll");
    if(!module)return reject("gameassembly-unavailable");
    auto domain_get=api<void*(*)()>(module,"il2cpp_domain_get");
    auto assembly_open=api<void*(*)(void*,const char*)>(module,"il2cpp_domain_assembly_open");
    auto assembly_image=api<void*(*)(void*)>(module,"il2cpp_assembly_get_image");
    auto find_class=api<void*(*)(void*,const char*,const char*)>(module,"il2cpp_class_from_name");
    auto methods=api<void*(*)(void*,void**)>(module,"il2cpp_class_get_methods");
    auto method_token=api<std::uint32_t(*)(void*)>(module,"il2cpp_method_get_token");
    auto arity=api<std::uint32_t(*)(void*)>(module,"il2cpp_method_get_param_count");
    auto name=api<const char*(*)(void*)>(module,"il2cpp_method_get_name");
    auto class_name=api<const char*(*)(void*)>(module,"il2cpp_class_get_name");
    auto class_namespace=api<const char*(*)(void*)>(module,"il2cpp_class_get_namespace");
    auto image_name=api<const char*(*)(void*)>(module,"il2cpp_image_get_name");
    auto flags=api<std::uint32_t(*)(void*,std::uint32_t*)>(module,"il2cpp_method_get_flags");
    auto return_type=api<void*(*)(void*)>(module,"il2cpp_method_get_return_type");
    auto type_kind=api<int(*)(void*)>(module,"il2cpp_type_get_type");
    auto assemblies=api<const void**(*)(void*,std::size_t*)>(module,"il2cpp_domain_get_assemblies");
    auto class_count=api<std::size_t(*)(void*)>(module,"il2cpp_image_get_class_count");
    auto image_class=api<void*(*)(void*,std::size_t)>(module,"il2cpp_image_get_class");
    object_class=api<void*(*)(void*)>(module,"il2cpp_object_get_class");
    if(!domain_get||!assembly_open||!assembly_image||!find_class||!methods||!method_token||!arity||!name||!flags||!return_type||!type_kind||!assemblies||!class_count||!image_class||!object_class||!class_name||!class_namespace||!image_name)return reject("required-il2cpp-export-unavailable");
    auto domain=domain_get();
    if(!domain)return reject("domain-unavailable");
    auto assembly=assembly_open(domain,"UniTask.dll");
    if(!assembly)return reject("unitask-assembly-unavailable");
    auto klass=find_class(assembly_image(assembly),"Cysharp.Threading.Tasks.Internal","PlayerLoopRunner");
    if(!klass)return reject("player-loop-class-unavailable");
    void* iterator{};void* method{};
    while(auto candidate=methods(klass,&iterator)){
        std::uint32_t implementation_flags{};
        if(method_token(candidate)==0x06000CD5&&arity(candidate)==0&&!std::strcmp(name(candidate),"Run")&&
            !(flags(candidate,&implementation_flags)&0x10U)&&type_kind(return_type(candidate))==1)method=candidate;
    }
    if(!method)return reject("player-loop-method-contract-mismatch");
    mark("target_method_info",reinterpret_cast<std::uintptr_t>(method));
    auto pointer=*static_cast<void**>(method);
    if(!pointer)return reject("player-loop-native-pointer-unavailable");
    mark("target_native_pointer",reinterpret_cast<std::uintptr_t>(pointer));
    std::size_t assembly_count{};const auto loaded=assemblies(domain,&assembly_count);
    if(!loaded||assembly_count>4096)return reject("assembly-inventory-invalid");
    mark("assembly_count",assembly_count);
    std::size_t matches{};
    nlohmann::json aliases=nlohmann::json::array();
    std::vector<void*> approved_aliases;
    bool unsupported_alias=false;
    for(std::size_t a=0;a<assembly_count;++a){
        auto image=assembly_image(const_cast<void*>(loaded[a]));
        if(!image)return reject("assembly-image-unavailable");
        const auto count=class_count(image);
        if(count>100000)return reject("class-inventory-invalid");
        for(std::size_t c=0;c<count;++c){
            auto type=image_class(image,c);void* cursor{};
            if(!type)continue;
            while(auto candidate=methods(type,&cursor)){
                if(*static_cast<void**>(candidate)==pointer){
                    ++matches;
                    aliases.push_back({{"image",image_name(image)},{"namespace",class_namespace(type)},{"class",class_name(type)},{"method",name(candidate)},
                        {"token",method_token(candidate)},{"arity",arity(candidate)},{"method_info",reinterpret_cast<std::uintptr_t>(candidate)}});
                    mark("aliases",aliases);mark("native_pointer_match_count",matches);
                    std::uint32_t implementation_flags{};
                    const auto token=method_token(candidate);
                    const auto index=token>=0x06000CD5?token-0x06000CD5:0xFFFFFFFFU;
                    const bool named_wrapper=index<player_loop_wrappers.size()&&!std::strcmp(name(candidate),player_loop_wrappers[index]);
                    const bool shape=type==klass&&arity(candidate)==0&&!(flags(candidate,&implementation_flags)&0x10U)&&type_kind(return_type(candidate))==1;
                    if(!shape||!named_wrapper)unsupported_alias=true;
                    else approved_aliases.push_back(candidate);
                    if(matches>32)return reject("native-alias-count-exceeds-bound");
                }
            }
        }
    }
    if(!matches)return reject("native-pointer-not-in-inventory");
    if(unsupported_alias||matches>player_loop_wrappers.size()||approved_aliases.size()!=matches)return reject("unapproved-shared-native-method-body");
    player_loop_class=klass;approved_player_loop_methods=std::move(approved_aliases);
    nlohmann::json approved=nlohmann::json::array();for(auto value:approved_player_loop_methods)approved.push_back(reinterpret_cast<std::uintptr_t>(value));
    mark("approved_method_infos",approved);mark("approved_alias_count",approved.size());
    mark("resolve_result",matches==1?"unique-contract-resolved":"pinned-unitask-phase-wrapper-group-resolved");
    return pointer;
    } catch(...) {return reject("native-resolver-exception");}
#else
    return nullptr;
#endif
}
void pump_from_player_loop(void* instance,void* method) noexcept {
#if defined(GKMS_RUNTIME_COMMAND_BRIDGE)
    mark("first_hook_callback",{{"thread_id",GetCurrentThreadId()},{"method_info",reinterpret_cast<std::uintptr_t>(method)}},true);
    if(!instance||!player_loop_class||!object_class){mark("first_skip_reason","receiver-or-contract-unavailable",true);return;}
    if(!method||std::find(approved_player_loop_methods.begin(),approved_player_loop_methods.end(),method)==approved_player_loop_methods.end()){
        mark("first_skip_reason","method-info-mismatch",true);return;
    }
    if(object_class(instance)!=player_loop_class){mark("first_skip_reason","receiver-class-mismatch",true);return;}
    mark("first_accepted_player_loop_thread_id",GetCurrentThreadId(),true);
    // The verified receiver/method boundary is available even when optional
    // translation hooks are disabled. The recorder retains its original once gate.
    try_start_gkms_telemetry_on_managed_thread();
    pump();
#endif
}
void record_player_loop_registration(int create_status,int enable_status) noexcept {
    mark("registration",{{"create_status",create_status},{"enable_status",enable_status},{"registered",create_status==0&&enable_status==0}});
}
void pump() noexcept {
#if defined(GKMS_RUNTIME_COMMAND_BRIDGE)
    try {
    // This is called only from the existing CampusActorController.LateUpdate
    // managed callback. No remote thread or native worker enters IL2CPP.
    static HMODULE module{};
    static DWORD (WINAPI *start)(const wchar_t*){};
    static DWORD (WINAPI *tick)(){};
    static bool started=false;
    static ULONGLONG next_tick{};
    const auto now=GetTickCount64();
    if(now<next_tick) return;
    next_tick=now+25;
    mark("first_bridge_pump_thread_id",GetCurrentThreadId(),true);
    if(!module) {
        const auto library=
            gkms::public_runtime_paths::native_root()/L"gkms_runtime_command_bridge.dll";
        module=LoadLibraryExW(library.c_str(),nullptr,
            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if(!module) {mark("load_library_error",GetLastError());next_tick=now+1000;return;}
        auto protocol=reinterpret_cast<unsigned int (WINAPI*)()>(GetProcAddress(module,"GKMSRuntimeCommandBridgeProtocolVersion"));
        start=reinterpret_cast<DWORD (WINAPI*)(const wchar_t*)>(GetProcAddress(module,"GKMSRuntimeCommandBridgeStartOnManagedThread"));
        tick=reinterpret_cast<DWORD (WINAPI*)()>(GetProcAddress(module,"GKMSRuntimeCommandBridgePumpOnManagedThread"));
        if(!protocol||protocol()!=1||!start||!tick) {mark("bridge_export_contract","mismatch");start=nullptr;tick=nullptr;return;}
        mark("bridge_export_contract","verified");
    }
    if(!start||!tick) return;
    if(!started) {
        const auto state=
            gkms::public_runtime_paths::state_root()/L"runtime_command_bridge";
        const auto result=start(state.c_str());
        mark("bridge_start_result",result);
        // A preflight rejection (2) still provides read-only status replies.
        started=result==0||result==2;
        if(!started) {next_tick=now+1000;return;}
    }
    tick();
    } catch (...) { mark("bridge_pump_error","portable-path-or-native-pump-exception"); }
#endif
}
}
