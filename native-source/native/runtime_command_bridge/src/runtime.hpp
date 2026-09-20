#pragma once
#include <Windows.h>
#include <cstdint>
#include <cstring>
#include <initializer_list>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>
#include "gc_handle_abi.hpp"

namespace gkms::bridge {
using json = nlohmann::json;

// All managed operations are invoked synchronously from the registered game
// thread. No managed object may be passed to an I/O/background worker.
class Runtime final {
public:
    bool initialize();
    void* klass(const char* assembly, const char* ns, const char* name);
    void* parent(void* klass);
    void* object_class(void* object);
    std::string class_name(void* klass);
    std::string class_namespace(void* klass);
    void* nested_class(void* klass, const char* name);
    void* method(void* klass, const char* name, int arity, std::uint32_t token = 0);
    json method_parameter_contract(void* method);
    void* invoke(void* method, void* instance, std::initializer_list<void*> args = {});
    void* getter(void* object, const char* name);
    void* new_object(void* klass);
    void* new_string(const char* utf8);
    void* reflection_type(void* klass);
    void* read_object_field(void* object, const char* name);
    // Boxes value-type fields through IL2CPP and roots the returned object
    // for the current operation. Never interpret a struct as an object pointer.
    void* read_boxed_field(void* object, const char* name);
    std::optional<std::int32_t> nullable_int32_field(void* object, const char* name);
    bool has_field(void* klass, const char* name);
    void field_value(void* object, const char* name, void* destination);
    std::string string(void* managed_string);
    std::vector<void*> enumerate(void* collection, std::size_t maximum = 10000);
    void* unbox_pointer(void* boxed);
    GCHandle weak_handle(void* object);
    GCHandle strong_handle(void* object);
    void* handle_target(GCHandle handle);
    void free_handle(GCHandle handle);
    void* method_pointer(void* method);
    bool managed_thread() const;
    bool operation_active() const noexcept { return operation_active_; }
    void begin_operation();
    void end_operation() noexcept;
    template<class T> T unbox(void* boxed) {
        T value{};
        std::memcpy(&value, unbox_pointer(boxed), sizeof(T));
        return value;
    }
    template<class T> T field(void* object, const char* name) {
        T value{}; field_value(object, name, &value); return value;
    }
private:
    HMODULE module_{};
    DWORD thread_id_{};
    bool operation_active_{};
    std::vector<GCHandle> operation_roots_;
    void pin_for_operation(void* object);
    template<class T> T api(const char* name) const {
        const auto address = GetProcAddress(module_, name);
        if (!address) throw std::runtime_error(std::string("missing IL2CPP export: ") + name);
        return reinterpret_cast<T>(address);
    }
};

class ManagedOperation final {
public:
    explicit ManagedOperation(Runtime& runtime):runtime_(runtime){runtime_.begin_operation();}
    ~ManagedOperation(){runtime_.end_operation();}
    ManagedOperation(const ManagedOperation&)=delete;
    ManagedOperation& operator=(const ManagedOperation&)=delete;
private: Runtime& runtime_;
};

std::string sha256(const std::string& bytes);
std::string utc_now();
} // namespace gkms::bridge
