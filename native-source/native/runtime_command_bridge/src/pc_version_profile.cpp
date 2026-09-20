#include "pc_version_profile.hpp"
#include <Windows.h>
#include <bcrypt.h>
#include <algorithm>
#include <array>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <vector>
#pragma comment(lib,"Bcrypt.lib")

namespace gkms::bridge {
namespace {
using json=nlohmann::json;
constexpr std::size_t max_metadata_bytes=128U*1024U*1024U;
void require(bool valid,const char* reason){if(!valid)throw std::runtime_error(reason);}
template<class T> T api(HMODULE module,const char* name){
    const auto value=GetProcAddress(module,name);
    if(!value)throw std::runtime_error(std::string("missing read-only IL2CPP export: ")+name);
    return reinterpret_cast<T>(value);
}
struct File final{HANDLE handle{INVALID_HANDLE_VALUE};~File(){if(handle!=INVALID_HANDLE_VALUE)CloseHandle(handle);}};
struct Algorithm final{BCRYPT_ALG_HANDLE handle{};~Algorithm(){if(handle)BCryptCloseAlgorithmProvider(handle,0);}};
struct Hash final{BCRYPT_HASH_HANDLE handle{};~Hash(){if(handle)BCryptDestroyHash(handle);}};
json file_identity(const BY_HANDLE_FILE_INFORMATION& info){
    return {{"volume_serial",info.dwVolumeSerialNumber},{"file_index_high",info.nFileIndexHigh},
        {"file_index_low",info.nFileIndexLow},{"size",(std::uint64_t(info.nFileSizeHigh)<<32)|info.nFileSizeLow},
        {"last_write_high",info.ftLastWriteTime.dwHighDateTime},{"last_write_low",info.ftLastWriteTime.dwLowDateTime}};
}
std::string hex(const std::array<unsigned char,32>& value){
    static constexpr char digits[]="0123456789abcdef";std::string result;result.reserve(64);
    for(auto byte:value){result.push_back(digits[byte>>4]);result.push_back(digits[byte&15]);}return result;
}
std::filesystem::path module_path(HMODULE module){
    std::array<wchar_t,32768> path{};const auto length=GetModuleFileNameW(module,path.data(),static_cast<DWORD>(path.size()));
    require(length>0&&length<path.size(),"current game module path unavailable");return std::filesystem::path(std::wstring(path.data(),length));
}
std::string text(const char* value){require(value!=nullptr,"null IL2CPP metadata string");return value;}
std::string query_text(const json& query,const char* key){
    require(query.contains(key)&&query.at(key).is_string(),"read-only query string required");
    const auto value=query.at(key).get<std::string>();
    require(value.size()<=1024&&value.find('\0')==std::string::npos,"read-only query string exceeds limits");return value;
}
json type_contract(HMODULE module,void* type){
    require(type!=nullptr,"null IL2CPP type descriptor");
    const auto release=api<void(*)(void*)>(module,"il2cpp_free");
    const std::unique_ptr<char,void(*)(void*)> name(api<char*(*)(void*)>(module,"il2cpp_type_get_name")(type),release);
    require(name&&*name,"type name unavailable");
    return {{"name",name.get()},{"kind",api<int(*)(void*)>(module,"il2cpp_type_get_type")(type)},
        {"byref",api<bool(*)(void*)>(module,"il2cpp_type_is_byref")(type)},
        {"attrs",api<std::uint32_t(*)(void*)>(module,"il2cpp_type_get_attrs")(type)}};
}
json class_identity(HMODULE module,void* klass){
    require(klass!=nullptr,"class identity unavailable");
    auto image=api<void*(*)(void*)>(module,"il2cpp_class_get_image")(klass);
    const auto parent=api<void*(*)(void*)>(module,"il2cpp_class_get_declaring_type");
    const auto name=api<const char*(*)(void*)>(module,"il2cpp_class_get_name");
    json path=json::array();auto outer=klass;std::vector<std::string> names;
    for(auto current=klass;current;current=parent(current)){
        require(names.size()<32,"nested type chain exceeds limit");names.push_back(text(name(current)));outer=current;
    }
    for(auto it=names.rbegin();it!=names.rend();++it)path.push_back(*it);
    return {{"image",text(api<const char*(*)(void*)>(module,"il2cpp_image_get_name")(image))},
        {"namespace",text(api<const char*(*)(void*)>(module,"il2cpp_class_get_namespace")(outer))},{"type_path",path}};
}
void* find_class(HMODULE module,const json& query){
    const auto image_name=query_text(query,"image"),ns=query_text(query,"namespace");
    const auto& path=query.at("type_path");require(path.is_array()&&!path.empty()&&path.size()<=32,"bounded declaring type path required");
    std::vector<std::string> names;for(const auto& value:path){require(value.is_string(),"type path component must be a string");
        auto part=value.get<std::string>();require(!part.empty()&&part.size()<=1024&&part.find('\0')==std::string::npos,"invalid type path component");names.push_back(part);}
    auto domain=api<void*(*)()>(module,"il2cpp_domain_get")();require(domain!=nullptr,"IL2CPP domain unavailable");
    std::size_t count{};auto assemblies=api<const void**(*)(void*,std::size_t*)>(module,"il2cpp_domain_get_assemblies")(domain,&count);
    require(assemblies&&count>0&&count<=4096,"loaded assembly inventory unavailable or unbounded");
    void* image{};
    for(std::size_t i=0;i<count;++i){auto candidate=api<void*(*)(const void*)>(module,"il2cpp_assembly_get_image")(assemblies[i]);
        if(text(api<const char*(*)(void*)>(module,"il2cpp_image_get_name")(candidate))!=image_name)continue;
        require(!image,"loaded assembly name ambiguous");image=candidate;}
    require(image!=nullptr,"requested assembly is not already loaded");
    void* klass=api<void*(*)(void*,const char*,const char*)>(module,"il2cpp_class_from_name")(image,ns.c_str(),names.front().c_str());
    require(klass!=nullptr,"requested declaring type not found");
    for(std::size_t depth=1;depth<names.size();++depth){void* iterator{};void* found{};std::size_t bounded{};
        while(auto nested=api<void*(*)(void*,void**)>(module,"il2cpp_class_get_nested_types")(klass,&iterator)){
            require(++bounded<=4096,"nested type inventory exceeds limit");
            if(text(api<const char*(*)(void*)>(module,"il2cpp_class_get_name")(nested))!=names[depth])continue;
            require(!found,"nested declaring type ambiguous");found=nested;}
        require(found!=nullptr,"nested declaring type not found");klass=found;}
    const json expected={{"image",image_name},{"namespace",ns},{"type_path",path}};
    require(class_identity(module,klass)==expected,"resolved declaring class identity differs");return klass;
}
json inspect_method(HMODULE module,const json& query){
    auto klass=find_class(module,query);const auto name=query_text(query,"name");
    require(query.at("arity").is_number_integer()&&query.at("token").is_number_unsigned(),"typed arity and exact current token required");
    const int arity=query.at("arity").get<int>();const auto token=query.at("token").get<std::uint32_t>();
    require(arity>=0&&arity<=128&&(token>>24)==6,"invalid arity or method token");
    void* iterator{};void* method{};std::size_t bounded{};
    while(auto candidate=api<void*(*)(void*,void**)>(module,"il2cpp_class_get_methods")(klass,&iterator)){
        require(++bounded<=16384,"declared method inventory exceeds limit");
        if(api<std::uint32_t(*)(void*)>(module,"il2cpp_method_get_token")(candidate)!=token)continue;
        require(!method,"current method token ambiguous");method=candidate;}
    require(method!=nullptr,"exact current method token not found on declaring class");
    require(text(api<const char*(*)(void*)>(module,"il2cpp_method_get_name")(method))==name,"current method name differs from request");
    require(api<std::uint32_t(*)(void*)>(module,"il2cpp_method_get_param_count")(method)==static_cast<std::uint32_t>(arity),"current method arity differs");
    require(class_identity(module,api<void*(*)(void*)>(module,"il2cpp_method_get_class")(method))==class_identity(module,klass),"method declaring owner differs");
    std::uint32_t implementation_flags{};const auto flags=api<std::uint32_t(*)(void*,std::uint32_t*)>(module,"il2cpp_method_get_flags")(method,&implementation_flags);
    json params=json::array();
    for(int index=0;index<arity;++index)params.push_back({{"index",index},
        {"name",text(api<const char*(*)(void*,std::uint32_t)>(module,"il2cpp_method_get_param_name")(method,index))},
        {"type",type_contract(module,api<void*(*)(void*,std::uint32_t)>(module,"il2cpp_method_get_param")(method,index))}});
    json result=class_identity(module,klass);result.update({{"name",name},{"arity",arity},{"token",token},
        {"flags",flags},{"implementation_flags",implementation_flags},{"static",(flags&0x10)!=0},
        {"return_type",type_contract(module,api<void*(*)(void*)>(module,"il2cpp_method_get_return_type")(method))},
        {"parameters",params},{"native_ABI_verified",false},{"mutation_qualified",false},{"managed_method_invoked",false}});
    return result;
}
json inspect_class(HMODULE module,const json& query){
    auto klass=find_class(module,query);json result=class_identity(module,klass),fields=json::array();
    void* iterator{};std::size_t bounded{};
    while(auto field=api<void*(*)(void*,void**)>(module,"il2cpp_class_get_fields")(klass,&iterator)){
        require(++bounded<=8192,"declared field inventory exceeds limit");
        fields.push_back({{"name",text(api<const char*(*)(void*)>(module,"il2cpp_field_get_name")(field))},
            {"type",type_contract(module,api<void*(*)(void*)>(module,"il2cpp_field_get_type")(field))},
            {"flags",api<int(*)(void*)>(module,"il2cpp_field_get_flags")(field)},
            {"offset",api<std::size_t(*)(void*)>(module,"il2cpp_field_get_offset")(field)}});
    }
    const bool value_type=api<bool(*)(void*)>(module,"il2cpp_class_is_valuetype")(klass);
    result.update({{"fields",fields},{"instance_size",api<std::int32_t(*)(void*)>(module,"il2cpp_class_instance_size")(klass)},
        {"is_value_type",value_type},{"is_enum",api<bool(*)(void*)>(module,"il2cpp_class_is_enum")(klass)},
        {"instance_values_read",false},{"serializer_equivalence_verified",false},{"mutation_qualified",false}});
    if(value_type){std::uint32_t alignment{};const auto size=api<std::int32_t(*)(void*,std::uint32_t*)>(module,"il2cpp_class_value_size")(klass,&alignment);
        result["value_size"]=size;result["value_alignment"]=alignment;}
    return result;
}
}

nlohmann::json inspect_pc_metadata_file(const std::filesystem::path& path){
    File file{CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr)};
    require(file.handle!=INVALID_HANDLE_VALUE,"metadata file cannot be opened without write/delete sharing");
    BY_HANDLE_FILE_INFORMATION before{},after{};require(GetFileInformationByHandle(file.handle,&before)!=0,"metadata file identity unavailable");
    const auto size=(std::uint64_t(before.nFileSizeHigh)<<32)|before.nFileSizeLow;
    require(size>=256&&size<=max_metadata_bytes,"metadata file size outside bounded supported range");
    Algorithm algorithm;require(BCryptOpenAlgorithmProvider(&algorithm.handle,BCRYPT_SHA256_ALGORITHM,nullptr,0)>=0,"metadata SHA256 provider unavailable");
    Hash hash;require(BCryptCreateHash(algorithm.handle,&hash.handle,nullptr,0,nullptr,0,0)>=0,"metadata SHA256 initialization failed");
    std::array<unsigned char,65536> buffer{};std::array<unsigned char,8> header{};std::uint64_t total{};
    while(true){DWORD count{};require(ReadFile(file.handle,buffer.data(),static_cast<DWORD>(buffer.size()),&count,nullptr)!=0,"metadata read failed");
        if(!count)break;if(!total){require(count>=header.size(),"metadata header truncated");std::copy_n(buffer.begin(),header.size(),header.begin());}
        total+=count;require(total<=size,"metadata size changed during read");
        require(BCryptHashData(hash.handle,buffer.data(),count,0)>=0,"metadata SHA256 update failed");}
    std::array<unsigned char,32> digest{};require(BCryptFinishHash(hash.handle,digest.data(),static_cast<ULONG>(digest.size()),0)>=0,"metadata SHA256 finalization failed");
    require(total==size&&GetFileInformationByHandle(file.handle,&after)!=0&&file_identity(before)==file_identity(after),"metadata identity changed during read");
    std::uint32_t magic{},version{};std::memcpy(&magic,header.data(),4);std::memcpy(&version,header.data()+4,4);
    require(magic==0xFAB11BAF&&version==31,"unsupported actual metadata magic/version");
    return {{"path",path.string()},{"sha256",hex(digest)},{"bytes",size},{"magic",magic},{"version",version},
        {"identity_before",file_identity(before)},{"identity_after",file_identity(after)},{"stable_during_read",true}};
}

nlohmann::json observe_current_pc_version(){
    auto module=GetModuleHandleW(L"GameAssembly.dll");require(module!=nullptr,"GameAssembly must already be loaded; this observer never loads it");
    const auto* base=reinterpret_cast<const std::byte*>(module);const auto* dos=reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    require(dos->e_magic==IMAGE_DOS_SIGNATURE&&dos->e_lfanew>0&&dos->e_lfanew<1024*1024,"invalid loaded GameAssembly DOS header");
    const auto* nt=reinterpret_cast<const IMAGE_NT_HEADERS64*>(base+dos->e_lfanew);
    require(nt->Signature==IMAGE_NT_SIGNATURE&&nt->FileHeader.Machine==IMAGE_FILE_MACHINE_AMD64&&nt->OptionalHeader.Magic==IMAGE_NT_OPTIONAL_HDR64_MAGIC,"unsupported loaded GameAssembly PE header");
    const auto path=module_path(module).parent_path()/L"gakumas_Data/il2cpp_data/Metadata/global-metadata.dat";
    const auto metadata=inspect_pc_metadata_file(path);
    PcVersionIdentity identity{nt->OptionalHeader.SizeOfImage,nt->FileHeader.TimeDateStamp,metadata.at("sha256").get<std::string>()};
    const auto profile=classify_pc_version(identity);
    return {{"schema","gkms.observed-pc-version-profile.v1"},{"profile_id",profile.id},{"recognized",profile.recognized},
        {"read_only_inspection_allowed",profile.read_only_inspection_allowed},{"input_qualified",profile.input_qualified},
        {"engine_identity",{{"metadata_sha256",identity.metadata_sha256},{"game_assembly_image_size",identity.image_size},
            {"game_assembly_timestamp",identity.timestamp},{"identity_source","actual loaded AMD64 PE and full stable metadata file hash"}}},
        {"metadata_file",metadata},{"new_profile_calculation_equivalence_verified",false},
        {"new_profile_model_compatibility_verified",false},{"managed_method_invoked",false},{"game_input_submitted",false}};
}

bool pc_method_contract_matches(const nlohmann::json& observed,const nlohmann::json& expected){
    static constexpr const char* keys[]={"image","namespace","type_path","name","arity","token","static","return_type","parameters"};
    if(!observed.is_object()||!expected.is_object())return false;
    for(const auto* key:keys)if(!observed.contains(key)||!expected.contains(key)||observed.at(key)!=expected.at(key))return false;
    return true;
}

nlohmann::json inspect_current_pc_contracts(const nlohmann::json& request){
    require(request.is_object()&&request.value("schema","")=="gkms.pc-readonly-contract-request.v1","explicit read-only PC contract request required");
    const auto version=observe_current_pc_version();require(version.at("read_only_inspection_allowed")==true,"unknown PC identity cannot enter typed export inspection");
    auto module=GetModuleHandleW(L"GameAssembly.dll");
    require(api<void*(*)()>(module,"il2cpp_thread_current")()!=nullptr,"read-only contract inspection requires the already managed thread");
    json result={{"schema","gkms.pc-readonly-contract-report.v1"},{"version",version},{"methods",json::array()},
        {"classes",json::array()},{"errors",json::array()},{"managed_method_invoked",false},{"object_values_read",false},
        {"game_input_submitted",false},{"native_ABI_verified",false},{"mutation_qualified",false}};
    for(const auto* family:{"methods","classes"}){
        const auto queries=request.value(family,json::array());require(queries.is_array()&&queries.size()<=512,"bounded PC metadata query list required");
        for(std::size_t i=0;i<queries.size();++i)try{
            auto record=std::strcmp(family,"methods")==0?inspect_method(module,queries[i]):inspect_class(module,queries[i]);
            record["query_index"]=i;
            if(std::strcmp(family,"methods")==0&&queries[i].contains("expected"))record["matches_expected_contract"]=pc_method_contract_matches(record,queries[i].at("expected"));
            result[family].push_back(std::move(record));
        }catch(const std::exception& error){result["errors"].push_back({{"family",family},{"query_index",i},{"reason",error.what()}});}
    }
    const auto after=observe_current_pc_version();require(version==after,"PC consumer identity changed during read-only contract inspection");
    result["complete"]=result.at("errors").empty();result["version_stable"]=true;return result;
}
} // namespace gkms::bridge
