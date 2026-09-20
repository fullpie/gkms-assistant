#pragma once
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <nlohmann/json.hpp>

namespace gkms::bridge::detail {

// Standard IL2CPP metadata exports. The returned type name is an owned native
// allocation; parameter names and type descriptors remain runtime-owned.
struct MethodParameterContractApi final {
    using ParamCount = std::uint32_t(*)(const void*);
    using Param = const void*(*)(const void*,std::uint32_t);
    using ParamName = const char*(*)(const void*,std::uint32_t);
    using TypeName = char*(*)(const void*);
    using TypeKind = int(*)(const void*);
    using TypeByref = bool(*)(const void*);
    using TypeAttrs = std::uint32_t(*)(const void*);
    using Free = void(*)(void*);
    ParamCount param_count{};
    Param param{};
    ParamName param_name{};
    TypeName type_name{};
    TypeKind type_kind{};
    TypeByref type_byref{};
    TypeAttrs type_attrs{};
    Free free{};
};

inline nlohmann::json inspect_method_parameters(
    const void* method,const MethodParameterContractApi& api) {
    if(!method)throw std::runtime_error("null method parameter contract");
    const auto require=[](bool present,const char* name){
        if(!present)throw std::runtime_error(std::string("missing IL2CPP export: ")+name);
    };
    // Resolve/validate the allocator pair before requesting any owned string.
    require(api.param_count!=nullptr,"il2cpp_method_get_param_count");
    require(api.param!=nullptr,"il2cpp_method_get_param");
    require(api.param_name!=nullptr,"il2cpp_method_get_param_name");
    require(api.type_name!=nullptr,"il2cpp_type_get_name");
    require(api.type_kind!=nullptr,"il2cpp_type_get_type");
    require(api.type_byref!=nullptr,"il2cpp_type_is_byref");
    require(api.type_attrs!=nullptr,"il2cpp_type_get_attrs");
    require(api.free!=nullptr,"il2cpp_free");
    const auto count=api.param_count(method);
    auto parameters=nlohmann::json::array();
    for(std::uint32_t index=0;index<count;++index){
        const auto type=api.param(method,index);
        if(!type)throw std::runtime_error("null IL2CPP parameter type at index "+std::to_string(index));
        const auto name=api.param_name(method,index);
        if(!name||!*name)throw std::runtime_error("IL2CPP parameter name unavailable at index "+std::to_string(index));
        const std::unique_ptr<char,MethodParameterContractApi::Free> type_name(api.type_name(type),api.free);
        if(!type_name||!*type_name)throw std::runtime_error("IL2CPP parameter type name unavailable at index "+std::to_string(index));
        parameters.push_back({
            {"index",index},{"name",name},{"type_name",type_name.get()},
            {"type_kind",api.type_kind(type)},{"byref",api.type_byref(type)},
            {"attrs",api.type_attrs(type)},
        });
    }
    return {{"schema","gkms.il2cpp-method-parameter-contract.v1"},
        {"parameter_count",count},{"parameters",std::move(parameters)}};
}
} // namespace gkms::bridge::detail
