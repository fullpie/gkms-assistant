#pragma once

#include <Windows.h>

#include <cstdint>
#include <string>

namespace gkms::il2cpp {

struct Il2CppString {
    void* klass;
    void* monitor;
    std::int32_t length;
    wchar_t chars[1];
};

struct MethodInfo {
    void* method_pointer;
};

struct FieldInfo {
    const char* name;
    const void* type;
    void* parent;
    std::int32_t offset;
    std::uint32_t token;
};

class Api final {
public:
    bool initialize();
    [[nodiscard]] bool ready() const noexcept;
    [[nodiscard]] HMODULE game_assembly() const noexcept;
    [[nodiscard]] void* domain() const noexcept;
    [[nodiscard]] void* current_thread() const noexcept;

    [[nodiscard]] void* find_class(
        const char* assembly,
        const char* namespaze,
        const char* name) const;
    [[nodiscard]] MethodInfo* find_method(
        void* klass,
        const char* name,
        int argument_count) const;
    [[nodiscard]] FieldInfo* find_field(void* klass, const char* name) const;
    [[nodiscard]] std::uint32_t method_token(MethodInfo* method) const;
    [[nodiscard]] std::uint32_t method_parameter_count(MethodInfo* method) const;
    [[nodiscard]] void* object_class(void* object) const;
    [[nodiscard]] const char* class_name(void* klass) const;
    [[nodiscard]] const char* class_namespace(void* klass) const;

private:
    using domain_get_fn = void* (*)();
    using domain_assembly_open_fn = void* (*)(void*, const char*);
    using assembly_get_image_fn = void* (*)(void*);
    using class_from_name_fn = void* (*)(void*, const char*, const char*);
    using class_get_method_from_name_fn = MethodInfo* (*)(void*, const char*, int);
    using class_get_field_from_name_fn = FieldInfo* (*)(void*, const char*);
    using method_get_token_fn = std::uint32_t (*)(const MethodInfo*);
    using method_get_param_count_fn = std::uint32_t (*)(const MethodInfo*);
    using thread_current_fn = void* (*)();
    using object_get_class_fn = void* (*)(void*);
    using class_get_name_fn = const char* (*)(void*);
    using class_get_namespace_fn = const char* (*)(void*);

    HMODULE game_assembly_{};
    void* domain_{};
    domain_get_fn domain_get_{};
    domain_assembly_open_fn domain_assembly_open_{};
    assembly_get_image_fn assembly_get_image_{};
    class_from_name_fn class_from_name_{};
    class_get_method_from_name_fn class_get_method_from_name_{};
    class_get_field_from_name_fn class_get_field_from_name_{};
    method_get_token_fn method_get_token_{};
    method_get_param_count_fn method_get_param_count_{};
    thread_current_fn thread_current_{};
    object_get_class_fn object_get_class_{};
    class_get_name_fn class_get_name_{};
    class_get_namespace_fn class_get_namespace_{};
};

std::string utf8(Il2CppString* value);

template <typename T>
bool call_instance_0(MethodInfo* method, void* instance, T& result) noexcept {
    if (method == nullptr || method->method_pointer == nullptr || instance == nullptr) {
        return false;
    }
    __try {
        using function_type = T (*)(void*, const MethodInfo*);
        result = reinterpret_cast<function_type>(method->method_pointer)(instance, method);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

// Invoke a managed instance method with one value argument while the caller is
// already on a registered IL2CPP thread.  The returned value is copied into a
// native object before the managed pointer can become stale; this is used for
// List<T>.get_Item(int) during the synchronous hand snapshot.
template <typename T, typename A>
bool call_instance_1(
    MethodInfo* method,
    void* instance,
    A argument,
    T& result) noexcept {
    if (method == nullptr || method->method_pointer == nullptr || instance == nullptr) {
        return false;
    }
    __try {
        using function_type = T (*)(void*, A, const MethodInfo*);
        result = reinterpret_cast<function_type>(method->method_pointer)(
            instance, argument, method);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

template <typename T>
bool read_field(void* instance, FieldInfo* field, T& result) noexcept {
    if (instance == nullptr || field == nullptr || field->offset < 0) {
        return false;
    }
    __try {
        result = *reinterpret_cast<T*>(
            reinterpret_cast<std::uint8_t*>(instance) + field->offset);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

}  // namespace gkms::il2cpp
