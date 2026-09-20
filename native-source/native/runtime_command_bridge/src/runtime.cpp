#include "runtime.hpp"
#include "native_trace.hpp"
#include "method_parameter_contract.hpp"
#include "managed_field_kind.hpp"
#include "pc_method_binding.hpp"
#include <bcrypt.h>
#include <array>
#include <iomanip>
#include <memory>
#include <sstream>

namespace gkms::bridge {
bool Runtime::initialize() {
    module_ = GetModuleHandleW(L"GameAssembly.dll");
    if (!module_) return false;
    if (!api<void*(*)()>("il2cpp_thread_current")()) return false;
    thread_id_ = GetCurrentThreadId();
    return true;
}
bool Runtime::managed_thread() const {
    return module_ && GetCurrentThreadId() == thread_id_ &&
        api<void*(*)()>("il2cpp_thread_current")() != nullptr;
}
void Runtime::begin_operation(){
    if(!managed_thread()||operation_active_)throw std::runtime_error("nested or wrong-thread managed operation");
    operation_active_=true;
}
void Runtime::end_operation() noexcept {
    trace_mark("gc.release_scope.before",{{"count",operation_roots_.size()}});
    try{
        bool first=true;
        for(auto handle:operation_roots_){
            if(first)trace_mark("gc.free_first.before",{{"handle",reinterpret_cast<std::uintptr_t>(handle)}});
            free_handle(handle);
            if(first)trace_mark("gc.free_first.after");
            first=false;
        }
    }catch(...){ }
    operation_roots_.clear();operation_active_=false;
    trace_mark("gc.release_scope.after");
}
void Runtime::pin_for_operation(void* object){
    if(!object||!operation_active_)return;
    const auto handle=api<GCHandleNew>("il2cpp_gchandle_new")(object,false);
    if(!handle)throw std::runtime_error("transient managed GC root allocation failed");
    if(operation_roots_.empty())trace_mark("gc.root_first",{{"handle",reinterpret_cast<std::uintptr_t>(handle)},{"object",reinterpret_cast<std::uintptr_t>(object)}});
    try{operation_roots_.push_back(handle);}catch(...){free_handle(handle);throw;}
}
void* Runtime::klass(const char* assembly, const char* ns, const char* name) {
    const auto domain = api<void*(*)()>("il2cpp_domain_get")();
    const auto loaded = api<void*(*)(void*,const char*)>("il2cpp_domain_assembly_open")(domain,assembly);
    const auto image = loaded ? api<void*(*)(void*)>("il2cpp_assembly_get_image")(loaded) : nullptr;
    const auto result = image ? api<void*(*)(void*,const char*,const char*)>("il2cpp_class_from_name")(image,ns,name) : nullptr;
    if (!result) throw std::runtime_error(std::string("class unavailable: ") + ns + "." + name);
    return result;
}
void* Runtime::parent(void* klass) { return api<void*(*)(void*)>("il2cpp_class_get_parent")(klass); }
void* Runtime::object_class(void* object) {
    if (!object) throw std::runtime_error("null managed object");
    return api<void*(*)(void*)>("il2cpp_object_get_class")(object);
}
std::string Runtime::class_name(void* klass) {
    return api<const char*(*)(void*)>("il2cpp_class_get_name")(klass);
}
std::string Runtime::class_namespace(void* klass) {
    return api<const char*(*)(void*)>("il2cpp_class_get_namespace")(klass);
}
void* Runtime::nested_class(void* klass,const char* name) {
    void* iterator{};void* found{};std::size_t count=0;
    while(auto nested=api<void*(*)(void*,void**)>("il2cpp_class_get_nested_types")(klass,&iterator)){
        if(++count>1024)throw std::runtime_error("nested class inventory exceeds limit");
        if(class_name(nested)!=name)continue;
        if(found)throw std::runtime_error("ambiguous nested class");
        found=nested;
    }
    if(!found)throw std::runtime_error(std::string("nested class unavailable: ")+name);
    return found;
}
void* Runtime::method(void* klass, const char* name, int arity, std::uint32_t token) {
    for (void* current = klass; current; current = parent(current)) {
        const auto selected_token = token ? pc_binding_token(current,name,arity,token) : 0;
        if (token && !selected_token) continue;
        void* iterator{};
        void* found{};
        while (auto candidate = api<void*(*)(void*,void**)>("il2cpp_class_get_methods")(current,&iterator)) {
            if (std::strcmp(api<const char*(*)(void*)>("il2cpp_method_get_name")(candidate),name)) continue;
            if (api<std::uint32_t(*)(void*)>("il2cpp_method_get_param_count")(candidate) != static_cast<std::uint32_t>(arity)) continue;
            if (selected_token && api<std::uint32_t(*)(void*)>("il2cpp_method_get_token")(candidate) != selected_token) continue;
            if (found) throw std::runtime_error(std::string("ambiguous method: ") + name);
            found = candidate;
        }
        if (found) return found;
    }
    throw std::runtime_error(std::string("method contract unavailable: ") + name);
}
json Runtime::method_parameter_contract(void* method) {
    if(!managed_thread())throw std::runtime_error("method parameter inspection on wrong thread");
    if(!method)throw std::runtime_error("null method parameter contract");
    using Api=detail::MethodParameterContractApi;
    return detail::inspect_method_parameters(method,{
        api<Api::ParamCount>("il2cpp_method_get_param_count"),
        api<Api::Param>("il2cpp_method_get_param"),
        api<Api::ParamName>("il2cpp_method_get_param_name"),
        api<Api::TypeName>("il2cpp_type_get_name"),
        api<Api::TypeKind>("il2cpp_type_get_type"),
        api<Api::TypeByref>("il2cpp_type_is_byref"),
        api<Api::TypeAttrs>("il2cpp_type_get_attrs"),
        api<Api::Free>("il2cpp_free"),
    });
}
void* Runtime::invoke(void* method, void* instance, std::initializer_list<void*> arguments) {
    if (!managed_thread()) throw std::runtime_error("managed invocation on wrong thread");
    if (!method) throw std::runtime_error("null method");
    std::vector<void*> values(arguments);
    void* exception{};
    if (instance && api<bool(*)(void*)>("il2cpp_class_is_valuetype")(
            api<void*(*)(void*)>("il2cpp_method_get_class")(method)))
        instance = unbox_pointer(instance);
    auto result = api<void*(*)(void*,void*,void**,void**)>("il2cpp_runtime_invoke")(
        method,instance,values.empty() ? nullptr : values.data(),&exception);
    if (exception) throw std::runtime_error("managed invocation exception: " + class_name(object_class(exception)));
    pin_for_operation(result);
    return result;
}
void* Runtime::getter(void* object, const char* name) { return invoke(method(object_class(object),name,0),object); }
void* Runtime::new_object(void* klass) {
    auto object = api<void*(*)(void*)>("il2cpp_object_new")(klass);
    if (!object) throw std::runtime_error("managed allocation failed");
    pin_for_operation(object);
    return object;
}
void* Runtime::new_string(const char* utf8) {
    if(!managed_thread()||!utf8)throw std::runtime_error("managed string allocation unavailable");
    auto object=api<void*(*)(const char*)>("il2cpp_string_new")(utf8);
    if(!object)throw std::runtime_error("managed string allocation failed");
    pin_for_operation(object);return object;
}
void* Runtime::unbox_pointer(void* boxed) {
    if (!boxed) throw std::runtime_error("null boxed result");
    auto value = api<void*(*)(void*)>("il2cpp_object_unbox")(boxed);
    if (!value) throw std::runtime_error("managed unbox failed");
    return value;
}
void* Runtime::reflection_type(void* klass) {
    auto type=api<void*(*)(void*)>("il2cpp_class_get_type")(klass);
    auto object=api<void*(*)(void*)>("il2cpp_type_get_object")(type);
    if(!object)throw std::runtime_error("managed reflection Type unavailable");
    pin_for_operation(object);return object;
}
void Runtime::field_value(void* object, const char* name, void* destination) {
    void* field{};
    for (auto current = object_class(object); current && !field; current = parent(current))
        field = api<void*(*)(void*,const char*)>("il2cpp_class_get_field_from_name")(current,name);
    if (!field) throw std::runtime_error(std::string("field unavailable: ") + name);
    api<void(*)(void*,void*,void*)>("il2cpp_field_get_value")(object,field,destination);
}
bool Runtime::has_field(void* klass,const char* name){
    for(auto current=klass;current;current=parent(current))
        if(api<void*(*)(void*,const char*)>("il2cpp_class_get_field_from_name")(current,name))return true;
    return false;
}
void* Runtime::read_object_field(void* object, const char* name) { return field<void*>(object,name); }
void* Runtime::read_boxed_field(void* object,const char* name) {
    if(!managed_thread()||!operation_active_)throw std::runtime_error("boxed field read requires a managed operation");
    void* field{};
    for(auto current=object_class(object);current&&!field;current=parent(current))
        field=api<void*(*)(void*,const char*)>("il2cpp_class_get_field_from_name")(current,name);
    if(!field)throw std::runtime_error(std::string("field unavailable: ")+name);
    auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(field);
    const int kind=api<int(*)(void*)>("il2cpp_type_get_type")(type);
    if(!can_box_managed_field(kind,api<bool(*)(void*)>("il2cpp_type_is_byref")(type)))
        throw std::runtime_error("boxed field has unsupported pointer/byref/open-generic type");
    auto value=api<void*(*)(void*,void*)>("il2cpp_field_get_value_object")(field,object);
    pin_for_operation(value);
    return value;
}
std::optional<std::int32_t> Runtime::nullable_int32_field(void* object,const char* name) {
    if (!managed_thread() || !operation_active()) throw std::runtime_error("nullable field read requires managed operation");
    void* field{};
    for (auto current=object_class(object);current&&!field;current=parent(current))
        field=api<void*(*)(void*,const char*)>("il2cpp_class_get_field_from_name")(current,name);
    if (!field) throw std::runtime_error("nullable field unavailable");
    auto type=api<void*(*)(void*)>("il2cpp_field_get_type")(field);
    const auto release=api<void(*)(void*)>("il2cpp_free");
    const std::unique_ptr<char,void(*)(void*)> type_name(api<char*(*)(void*)>("il2cpp_type_get_name")(type),release);
    if (!type_name || std::strcmp(type_name.get(),"System.Nullable<System.Int32>") ||
        api<bool(*)(void*)>("il2cpp_type_is_byref")(type))
        throw std::runtime_error("field is not the expected closed nullable Int32");
    // CLR nullable boxing returns either null or the boxed underlying Int32.
    // This avoids assuming the in-memory hasValue/value struct offsets.
    auto value=read_boxed_field(object,name);
    if (!value) return std::nullopt;
    auto klass=object_class(value);
    std::uint32_t alignment{};
    if (class_namespace(klass)!="System" || class_name(klass)!="Int32" ||
        !api<bool(*)(void*)>("il2cpp_class_is_valuetype")(klass) ||
        api<std::int32_t(*)(void*,std::uint32_t*)>("il2cpp_class_value_size")(klass,&alignment)!=sizeof(std::int32_t))
        throw std::runtime_error("nullable field boxing did not return Int32");
    return unbox<std::int32_t>(value);
}
std::string Runtime::string(void* managed_string) {
    if (!managed_string) return {};
    const int length = api<std::int32_t(*)(void*)>("il2cpp_string_length")(managed_string);
    if (length < 0 || length > 16 * 1024 * 1024) throw std::runtime_error("managed string exceeds limit");
    if (!length) return {};
    auto chars = api<const wchar_t*(*)(void*)>("il2cpp_string_chars")(managed_string);
    const int size = WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,chars,length,nullptr,0,nullptr,nullptr);
    if (size <= 0) throw std::runtime_error("invalid managed UTF-16");
    std::string text(static_cast<std::size_t>(size),'\0');
    WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,chars,length,text.data(),size,nullptr,nullptr);
    return text;
}
std::vector<void*> Runtime::enumerate(void* collection, std::size_t maximum) {
    if (!collection) throw std::runtime_error("null collection");
    auto type = object_class(collection);
    std::vector<void*> values;
    if (api<std::uint32_t(*)(void*)>("il2cpp_class_get_rank")(type) == 1) {
        const auto count = api<std::uintptr_t(*)(void*)>("il2cpp_array_length")(collection);
        auto element = api<void*(*)(void*)>("il2cpp_class_get_element_class")(type);
        auto element_type=api<void*(*)(void*)>("il2cpp_class_get_type")(element);
        if(!managed_reference_element(api<int(*)(void*)>("il2cpp_type_get_type")(element_type),
            api<bool(*)(void*)>("il2cpp_type_is_byref")(element_type),
            api<bool(*)(void*)>("il2cpp_class_is_valuetype")(element)))
            throw std::runtime_error("array enumeration requires managed reference elements");
        if (count > maximum) throw std::runtime_error("collection exceeds limit");
        // Use the current runtime's public header size, as the recorder does.
        const auto header = api<std::uint32_t(*)()>("il2cpp_array_object_header_size")();
        if (header < 2*sizeof(void*) || header > 256 || header % alignof(void*) != 0)
            throw std::runtime_error("unsupported current managed array header size");
        const auto data = reinterpret_cast<void**>(static_cast<std::byte*>(collection) + header);
        values.assign(data,data+count);
        return values;
    }
    void* count_method{}; void* item_method{};
    try { count_method=method(type,"get_Count",0); item_method=method(type,"get_Item",1); } catch (const std::exception&) {}
    if (count_method && item_method) {
        const auto count = unbox<std::int32_t>(invoke(count_method,collection));
        if (count < 0 || static_cast<std::size_t>(count) > maximum) throw std::runtime_error("collection count invalid");
        for (std::int32_t index=0; index<count; ++index) values.push_back(invoke(item_method,collection,{&index}));
        return values;
    }
    auto enumerator = getter(collection,"GetEnumerator");
    auto move = method(object_class(enumerator),"MoveNext",0);
    auto current = method(object_class(enumerator),"get_Current",0);
    void* dispose{};
    try { dispose = method(object_class(enumerator),"Dispose",0); } catch (const std::exception&) {}
    try {
        while (unbox<bool>(invoke(move,enumerator))) {
            if (values.size() >= maximum) throw std::runtime_error("collection exceeds limit");
            values.push_back(invoke(current,enumerator));
        }
    } catch (...) { if (dispose) invoke(dispose,enumerator); throw; }
    if (dispose) invoke(dispose,enumerator);
    return values;
}
GCHandle Runtime::weak_handle(void* object) { return api<GCHandleNew>("il2cpp_gchandle_new_weakref")(object,false); }
GCHandle Runtime::strong_handle(void* object) {
    if(!managed_thread()||!object)throw std::runtime_error("persistent managed root unavailable");
    auto handle=api<GCHandleNew>("il2cpp_gchandle_new")(object,false);
    if(!handle)throw std::runtime_error("persistent managed root allocation failed");
    return handle;
}
void* Runtime::handle_target(GCHandle handle) { return handle ? api<GCHandleGetTarget>("il2cpp_gchandle_get_target")(handle) : nullptr; }
void Runtime::free_handle(GCHandle handle) { if (handle) api<GCHandleFree>("il2cpp_gchandle_free")(handle); }
void* Runtime::method_pointer(void* method) { return *static_cast<void**>(method); }

std::string sha256(const std::string& bytes) {
    std::array<unsigned char,32> digest{};
    BCRYPT_ALG_HANDLE algorithm{};
    if (BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0) < 0) throw std::runtime_error("SHA256 provider unavailable");
    const auto status = BCryptHash(algorithm,nullptr,0,
        reinterpret_cast<PUCHAR>(const_cast<char*>(bytes.data())),static_cast<ULONG>(bytes.size()),digest.data(),static_cast<ULONG>(digest.size()));
    BCryptCloseAlgorithmProvider(algorithm,0);
    if (status < 0) throw std::runtime_error("SHA256 failed");
    std::ostringstream output;
    for (auto byte : digest) output << std::hex << std::setfill('0') << std::setw(2) << static_cast<unsigned int>(byte);
    return output.str();
}
std::string utc_now() {
    SYSTEMTIME time{}; GetSystemTime(&time);
    char buffer[32]{};
    sprintf_s(buffer,"%04u-%02u-%02uT%02u:%02u:%02u.%03uZ",time.wYear,time.wMonth,time.wDay,time.wHour,time.wMinute,time.wSecond,time.wMilliseconds);
    return buffer;
}
} // namespace gkms::bridge
