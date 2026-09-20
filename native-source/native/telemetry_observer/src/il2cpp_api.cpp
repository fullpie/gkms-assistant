#include "il2cpp_api.hpp"

#include <array>
#include <vector>

namespace gkms::il2cpp {
namespace {

template <typename T>
T resolve(HMODULE module, const char* name) {
    return reinterpret_cast<T>(GetProcAddress(module, name));
}

bool readable_span(const void* value, std::size_t size) noexcept {
    if (value == nullptr || size == 0) {
        return false;
    }
    const auto start = reinterpret_cast<std::uintptr_t>(value);
    if (start + size < start) {
        return false;
    }
    std::uintptr_t cursor = start;
    const std::uintptr_t end = start + size;
    while (cursor < end) {
        MEMORY_BASIC_INFORMATION region{};
        if (VirtualQuery(reinterpret_cast<const void*>(cursor), &region, sizeof(region)) !=
            sizeof(region) || region.State != MEM_COMMIT ||
            (region.Protect & (PAGE_GUARD | PAGE_NOACCESS)) != 0) {
            return false;
        }
        const auto region_end = reinterpret_cast<std::uintptr_t>(region.BaseAddress) +
            region.RegionSize;
        if (region_end <= cursor) {
            return false;
        }
        cursor = region_end;
    }
    return true;
}

bool safe_string_view(
    Il2CppString* value,
    const wchar_t*& characters,
    int& length) noexcept {
    if (value == nullptr) {
        return false;
    }
    __try {
        length = value->length;
        characters = value->chars;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    constexpr int kMaximumTelemetryStringLength = 8192;
    return length > 0 && length <= kMaximumTelemetryStringLength &&
        readable_span(characters, static_cast<std::size_t>(length) * sizeof(wchar_t));
}

}  // namespace

bool Api::initialize() {
    game_assembly_ = GetModuleHandleW(L"GameAssembly.dll");
    if (game_assembly_ == nullptr) {
        return false;
    }
    domain_get_ = resolve<domain_get_fn>(game_assembly_, "il2cpp_domain_get");
    domain_assembly_open_ = resolve<domain_assembly_open_fn>(
        game_assembly_, "il2cpp_domain_assembly_open");
    assembly_get_image_ = resolve<assembly_get_image_fn>(
        game_assembly_, "il2cpp_assembly_get_image");
    class_from_name_ = resolve<class_from_name_fn>(
        game_assembly_, "il2cpp_class_from_name");
    class_get_method_from_name_ = resolve<class_get_method_from_name_fn>(
        game_assembly_, "il2cpp_class_get_method_from_name");
    class_get_field_from_name_ = resolve<class_get_field_from_name_fn>(
        game_assembly_, "il2cpp_class_get_field_from_name");
    method_get_token_ = resolve<method_get_token_fn>(
        game_assembly_, "il2cpp_method_get_token");
    method_get_param_count_ = resolve<method_get_param_count_fn>(
        game_assembly_, "il2cpp_method_get_param_count");
    thread_current_ = resolve<thread_current_fn>(
        game_assembly_, "il2cpp_thread_current");
    object_get_class_ = resolve<object_get_class_fn>(
        game_assembly_, "il2cpp_object_get_class");
    class_get_name_ = resolve<class_get_name_fn>(
        game_assembly_, "il2cpp_class_get_name");
    class_get_namespace_ = resolve<class_get_namespace_fn>(
        game_assembly_, "il2cpp_class_get_namespace");
    if (!ready()) {
        return false;
    }
    if (current_thread() == nullptr) {
        return false;
    }
    domain_ = domain_get_();
    return domain_ != nullptr;
}

bool Api::ready() const noexcept {
    return game_assembly_ != nullptr && domain_get_ != nullptr &&
        domain_assembly_open_ != nullptr && assembly_get_image_ != nullptr &&
        class_from_name_ != nullptr && class_get_method_from_name_ != nullptr &&
        class_get_field_from_name_ != nullptr && method_get_token_ != nullptr &&
        method_get_param_count_ != nullptr && thread_current_ != nullptr &&
        object_get_class_ != nullptr &&
        class_get_name_ != nullptr && class_get_namespace_ != nullptr;
}

HMODULE Api::game_assembly() const noexcept {
    return game_assembly_;
}

void* Api::domain() const noexcept {
    return domain_;
}

void* Api::current_thread() const noexcept {
    if (thread_current_ == nullptr) {
        return nullptr;
    }
    __try {
        return thread_current_();
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return nullptr;
    }
}

void* Api::find_class(
    const char* assembly,
    const char* namespaze,
    const char* name) const {
    if (!ready() || domain_ == nullptr) {
        return nullptr;
    }
    void* opened = domain_assembly_open_(domain_, assembly);
    if (opened == nullptr) {
        std::string with_extension = std::string(assembly) + ".dll";
        opened = domain_assembly_open_(domain_, with_extension.c_str());
    }
    if (opened == nullptr) {
        return nullptr;
    }
    void* image = assembly_get_image_(opened);
    return image == nullptr ? nullptr : class_from_name_(image, namespaze, name);
}

MethodInfo* Api::find_method(
    void* klass,
    const char* name,
    int argument_count) const {
    return klass == nullptr ? nullptr :
        class_get_method_from_name_(klass, name, argument_count);
}

FieldInfo* Api::find_field(void* klass, const char* name) const {
    return klass == nullptr ? nullptr : class_get_field_from_name_(klass, name);
}

std::uint32_t Api::method_token(MethodInfo* method) const {
    return method == nullptr ? 0 : method_get_token_(method);
}

std::uint32_t Api::method_parameter_count(MethodInfo* method) const {
    return method == nullptr ? 0 : method_get_param_count_(method);
}

void* Api::object_class(void* object) const {
    if (object == nullptr) {
        return nullptr;
    }
    __try {
        return object_get_class_(object);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return nullptr;
    }
}

const char* Api::class_name(void* klass) const {
    return klass == nullptr ? nullptr : class_get_name_(klass);
}

const char* Api::class_namespace(void* klass) const {
    return klass == nullptr ? nullptr : class_get_namespace_(klass);
}

std::string utf8(Il2CppString* value) {
    const wchar_t* characters{};
    int length{};
    if (!safe_string_view(value, characters, length)) {
        return {};
    }
    const int needed = WideCharToMultiByte(
        CP_UTF8,
        0,
        characters,
        length,
        nullptr,
        0,
        nullptr,
        nullptr);
    if (needed <= 0) {
        return {};
    }
    std::string result(static_cast<std::size_t>(needed), '\0');
    WideCharToMultiByte(
        CP_UTF8,
        0,
        characters,
        length,
        result.data(),
        needed,
        nullptr,
        nullptr);
    return result;
}

}  // namespace gkms::il2cpp
